"""Context-window auto-detection — makes the harness model-agnostic.

Any LLM model, any context window, must work without per-model hardcoding.
When the admin has NOT set ``context_window`` on the LlmModel catalog row,
we probe the endpoint itself for its real window:

- OpenAI-compatible ``GET {base_url}/models`` — vLLM returns
  ``max_model_len`` for each model; some gateways expose
  ``context_length`` / ``context_window`` / ``max_context_length``.
- Ollama native ``POST {base_url}/api/show`` — returns ``context_length``
  (the OpenAI-compatible /v1/models does NOT carry it).

Probed values are cached (TTL) and registered by model_id so every
consumer (compaction, pre-flight, tool-result persistence, data-source
runtime) resolves the SAME real window regardless of which code path asks.

Probing is best-effort: any error → None, never raises, never blocks
longer than a short timeout.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 2.0
_TTL_SECONDS = 600.0  # re-probe at most every 10 min per (base_url, model_id)

# model_id -> probed context window (tokens).  Written by the probe,
# read by get_context_window() so ALL call sites agree.
_registry: dict[str, int] = {}
# (base_url, model_id) -> (window, monotonic_ts)
_cache: dict[tuple[str, str], tuple[int, float]] = {}
_lock = threading.Lock()


def get_registered_context_window(model_id: str | None) -> int | None:
    """Return a previously-probed window for this model_id, or None."""
    if not model_id:
        return None
    with _lock:
        return _registry.get(model_id)


def probe_context_window(
    base_url: str | None,
    api_key: str | None,
    model_id: str | None,
    *,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> int | None:
    """Probe the endpoint's real context window for ``model_id``.

    Returns the window in tokens, or None if the endpoint does not expose
    it / is unreachable / errors.  Never raises.
    """
    if not base_url or not model_id:
        return None
    key = (base_url, model_id)
    with _lock:
        hit = _cache.get(key)
        if hit and (time.monotonic() - hit[1]) < _TTL_SECONDS:
            return hit[0]

    window = _probe_openai_compat(base_url, api_key, model_id, timeout)
    if window is None:
        window = _probe_ollama_native(base_url, model_id, timeout)

    if window:
        with _lock:
            _registry[model_id] = window
            _cache[key] = (window, time.monotonic())
        log.info(
            "Auto-detected context window for model %s: %d tokens",
            model_id, window,
        )
    else:
        # Cache the miss briefly so we do not hammer an endpoint that
        # does not expose its window (e.g. OpenAI/DashScope /v1/models).
        with _lock:
            _cache[key] = (0, time.monotonic())
    return window


def _probe_openai_compat(
    base_url: str, api_key: str | None, model_id: str, timeout: float,
) -> int | None:
    """GET {base_url}/models and read the model's context fields."""
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    url = f"{url}/models"
    headers = {"Authorization": f"Bearer {api_key or ''}"} if api_key else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — best-effort probe
        log.debug("context probe %s failed: %s", url, exc)
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get("id") or row.get("model") or ""
        # Exact match first, then suffix match ("-awq4" style variants).
        if mid != model_id and not mid.startswith(model_id):
            continue
        for field in (
            "max_model_len", "context_length", "context_window",
            "max_context_length", "max_input_tokens",
        ):
            val = row.get(field)
            if isinstance(val, int) and val > 0:
                return val
            if isinstance(val, str) and val.isdigit() and int(val) > 0:
                return int(val)
    return None


def _probe_ollama_native(
    base_url: str, model_id: str, timeout: float,
) -> int | None:
    """POST {base_url}/api/show {"model": ...} → context_length (Ollama)."""
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    url = f"{url}/api/show"
    try:
        resp = httpx.post(url, json={"model": model_id}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.debug("ollama probe %s failed: %s", url, exc)
        return None
    if not isinstance(data, dict):
        return None
    for field in ("context_length", "context_window", "max_context_length"):
        val = data.get(field)
        if isinstance(val, int) and val > 0:
            return val
        if isinstance(val, str) and val.isdigit() and int(val) > 0:
            return int(val)
    # Ollama >= 0.2.x nests under model_info["context_length"].
    model_info = data.get("model_info")
    if isinstance(model_info, dict):
        val = model_info.get("context_length")
        if isinstance(val, int) and val > 0:
            return val
    return None
