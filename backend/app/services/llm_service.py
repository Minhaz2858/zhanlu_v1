"""Shared LLM calling utility — extracted from integrations.py for reuse.

Both integrations.py (InvokeLLM, InvokeLLMStream) and agents.py (add_message)
use these helpers to call the configured OpenAI-compatible LLM (DeepSeek).
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import HTTPException

from app.config import settings
from app.services.tracing import get_tracer as _get_tracer
from app.services.provider_health import (
    is_healthy, record_success, record_failure, select_provider,
)

import time as _time

logger = logging.getLogger(__name__)


def build_llm_payload(body: dict, model: str) -> tuple[dict, list[dict], bool]:
    """Build the LLM API payload, messages, and json_schema flag from the request body."""
    prompt = body.get("prompt", "")
    temperature = body.get("temperature", 0.7)
    messages = body.get("messages")
    if not messages:
        messages = [{"role": "user", "content": prompt}]

    json_schema = body.get("response_json_schema")

    # DeepSeek requires the word "json" in the messages when using
    # response_format=json_object. Inject a system message to satisfy this.
    if json_schema:
        schema_hint = json.dumps(json_schema) if isinstance(json_schema, dict) else str(json_schema)
        messages = [{"role": "system", "content": f"You must respond with valid JSON matching this schema: {schema_hint}"}] + messages

    payload = {
        "model": model,
        "messages": messages,
    }
    if not model_has_fixed_temperature(model):
        payload["temperature"] = temperature
    if json_schema:
        payload["response_format"] = {"type": "json_object"}

    return payload, messages, bool(json_schema)


def _sanitize_tool_call_pairing(messages: list[dict] | None) -> list[dict]:
    """Drop orphan ``assistant.tool_calls`` and stray ``tool`` role messages.

    The OpenAI/DeepSeek API requires that every ``assistant`` message carrying
    ``tool_calls`` be immediately followed by one ``tool`` role message per
    call (matched by ``tool_call_id``).  When a message-rebuild or compaction
    path strips the tool-result messages but leaves the
    ``assistant.tool_calls`` field intact, the provider returns HTTP 400
    ("insufficient tool messages following tool_calls message").  That cascades
    into :func:`record_failure` -> circuit-breaker opens -> all providers
    exhausted -> 502 with no error message -> the user sees
    "agent not responding".

    This validator is a defense-in-depth gate: it sanitises the message list
    right before the HTTP request so that no upstream code path can produce
    an orphan-``tool_calls`` 400.  Two passes:

    **Pass 1 (forward scan)**: for each ``assistant`` message with
    ``tool_calls``, walk forward through *contiguous* ``tool`` role messages
    collecting matching ``tool_call_id``s.  Any unmatched ids are stripped
    from ``assistant.tool_calls``.  If the entire list is stripped, the key
    is removed.

    **Pass 2 (backward scan)**: any ``tool`` role message whose
    ``tool_call_id`` does not appear in *any* preceding ``assistant.tool_calls``
    is removed.

    Returns a new list (never mutates the input).
    """
    if not messages:
        return list(messages) if messages is not None else []

    # Pass 1: collect expected tool_call_ids per assistant position
    assistant_expected: dict[int, set[str]] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        tc = msg.get("tool_calls")
        if not tc:
            continue
        ids: set[str] = set()
        for c in tc:
            tid = c.get("id") or c.get("tool_call_id", "")
            if tid:
                ids.add(tid)
        if ids:
            assistant_expected[i] = ids

    # Walk forward from each assistant to find contiguous matching tool results
    matched_ids: set[str] = set()
    for asst_i, expected_ids in assistant_expected.items():
        found: set[str] = set()
        j = asst_i + 1
        while j < len(messages):
            nxt = messages[j]
            if nxt.get("role") != "tool":
                break  # tool results must be contiguous
            tid = nxt.get("tool_call_id", "")
            if tid in expected_ids:
                found.add(tid)
            j += 1
        matched_ids |= found

    # Build new list: strip unmatched tool_calls from assistant messages
    cleaned: list[dict] = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected = assistant_expected.get(i, set())
            kept = [
                c for c in msg["tool_calls"]
                if (c.get("id") or c.get("tool_call_id", "")) in expected
                and (c.get("id") or c.get("tool_call_id", "")) in matched_ids
            ]
            if kept:
                new_msg = {**msg, "tool_calls": kept}
            else:
                # All tool_calls were orphan — strip the key but keep text
                new_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
            if not new_msg.get("content"):
                new_msg.setdefault("content", "")
            cleaned.append(new_msg)
        else:
            cleaned.append(msg)

    # Pass 2: remove stray tool messages whose tool_call_id has no
    # preceding assistant.tool_calls entry
    all_assistant_tc_ids: set[str] = set()
    result: list[dict] = []
    for msg in cleaned:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for c in msg["tool_calls"]:
                tid = c.get("id") or c.get("tool_call_id", "")
                if tid:
                    all_assistant_tc_ids.add(tid)
        if msg.get("role") == "tool":
            tid = msg.get("tool_call_id", "")
            if tid not in all_assistant_tc_ids:
                continue
        result.append(msg)

    return result


def _supports_parallel_tool_calls(model: str) -> bool:
    """Return True if the LLM provider for ``model`` accepts the
    ``parallel_tool_calls`` capability flag.

    The OpenAI/DeepSeek API treats this as a hint to emit multiple
    ``tool_calls`` in a single assistant message.  Anthropic rejects the
    field (HTTP 400), so it must be excluded for any model whose name
    suggests the Claude family.

    Conservative default: if the model name is unrecognised, return
    False — no injection, no risk of breaking unknown providers.
    """
    if not model:
        return False
    m = model.lower()
    # Hard reject: anthropic / claude never accept this field.
    if "anthropic" in m or "claude" in m:
        return False
    # OpenAI-compatible families: deepseek, OpenAI direct, GPT-*.
    if "deepseek" in m:
        return True
    if m.startswith("openai/") or m.startswith("gpt-"):
        return True
    return False


def llm_headers() -> dict:
    """Return the authorization headers for the LLM API."""
    return {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


def llm_url() -> str:
    """Return the full URL for the LLM chat completions endpoint."""
    return f"{settings.OPENAI_BASE_URL.rstrip('/')}/chat/completions"


def extract_stream_parts(chunk: dict) -> tuple[str, str]:
    """Return (content_delta, reasoning_delta) from a provider stream chunk.

    Tolerant of multiple provider formats. Returns ("", "") when the chunk
    has neither content nor reasoning. Pure function — safe in any async ctx.

    Supported reasoning field names (in order of preference):
      - delta.reasoning_content (DeepSeek-R1)
      - delta.thinking         (Claude-style)
      - delta.reasoning        (OpenAI o1)
    """
    try:
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
    except (IndexError, AttributeError, TypeError):
        return ("", "")

    content = delta.get("content") or ""
    reasoning = (
        delta.get("reasoning_content")
        or delta.get("thinking")
        or delta.get("reasoning")
        or ""
    )
    # Normalize to str — providers may yield None for empty fields
    if content is None:
        content = ""
    if reasoning is None:
        reasoning = ""
    return (str(content), str(reasoning))


def get_model() -> str:
    """Return the configured LLM model name."""
    return settings.LLM_MODEL


def supports_json_schema(provider_name: str) -> bool:
    """Return True if the provider supports native ``response_format`` json_schema.

    Currently only OpenAI supports ``json_schema`` mode; DeepSeek and most
    other providers only support ``json_object`` mode (which requires the
    word "json" in the prompt). Returns False for unknown providers.
    """
    native_providers = {"openai", "gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-3.5-turbo"}
    return provider_name.lower() in native_providers


def model_has_fixed_temperature(model: str) -> bool:
    """Return True for models that reject a caller-supplied ``temperature``.

    Reasoning models such as Moonshot ``kimi-k2.6`` only accept ``temperature=1``
    and OpenAI ``o1``/``o3`` reject the field entirely. For these models we omit
    the temperature key so the provider's own default is used — otherwise a
    perfectly valid key surfaces as a misleading HTTP 400
    (``invalid temperature: only 1 is allowed for this model``).

    Matched case-insensitively as a substring against ``LLM_FIXED_TEMPERATURE_MODELS``.
    """
    if not model:
        return False
    m = model.lower()
    patterns = [
        p.strip().lower()
        for p in settings.LLM_FIXED_TEMPERATURE_MODELS.split(",")
        if p.strip()
    ]
    return any(p in m for p in patterns)


@dataclass
class LLMProvider:
    """A single OpenAI-compatible LLM endpoint for failover."""

    name: str
    base_url: str
    api_key: str
    model: str


def get_llm_providers() -> list[LLMProvider]:
    """Ordered list of LLM providers for failover.

    The primary provider is built from ``OPENAI_BASE_URL`` /
    ``OPENAI_API_KEY`` / ``LLM_MODEL``. Additional fallback providers are
    parsed from the ``LLM_FALLBACK_PROVIDERS`` setting — a JSON array of
    ``{"name", "base_url", "api_key", "model"}`` objects. Providers without
    a ``base_url`` or ``api_key`` are dropped (so a misconfigured fallback
    can't shadow a working primary). The first usable provider is primary;
    subsequent ones are tried in order on ``HTTPStatusError`` /
    ``RequestError``.

    With the default empty ``LLM_FALLBACK_PROVIDERS`` this returns a
    single-element list and behavior is identical to the pre-fallback code.
    """
    providers: list[LLMProvider] = [
        LLMProvider(
            name="primary",
            base_url=settings.OPENAI_BASE_URL,
            api_key=settings.OPENAI_API_KEY,
            model=settings.LLM_MODEL,
        )
    ]
    raw = getattr(settings, "LLM_FALLBACK_PROVIDERS", "") or ""
    if raw.strip():
        try:
            fb_list = json.loads(raw)
            if isinstance(fb_list, list):
                for i, fb in enumerate(fb_list):
                    if not isinstance(fb, dict):
                        continue
                    providers.append(
                        LLMProvider(
                            name=fb.get("name") or f"fallback-{i + 1}",
                            base_url=fb.get("base_url", ""),
                            api_key=fb.get("api_key", ""),
                            model=fb.get("model") or settings.LLM_MODEL,
                        )
                    )
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("LLM_FALLBACK_PROVIDERS parse failed (non-fatal): %s", e)
    return [p for p in providers if p.base_url and p.api_key]


def _provider_chat_url(provider: LLMProvider) -> str:
    return f"{provider.base_url.rstrip('/')}/chat/completions"


def _provider_headers(provider: LLMProvider) -> dict:
    return {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }


async def call_llm(
    prompt: str = "",
    messages: list[dict] | None = None,
    temperature: float = 0.7,
    response_json_schema: dict | None = None,
    task_type: str | None = None,
    endpoint: Optional[Any] = None,
) -> dict:
    """Call the configured LLM and return a parsed response dict.

    Integrates model routing (per-task model selection), provider health
    (circuit-breaker), and optional Redis response caching (temperature=0).

    ``endpoint`` (an ``LLMEndpoint`` from hierarchical LLM resolution) pins
    the call to a specific provider+model: base_url / api_key / model_id are
    used verbatim and task-based routing, provider failover, health routing
    and response caching are bypassed.  When ``None`` (default), the legacy
    behavior applies.

    Returns:
        dict with keys:
            - response (str): the raw text response or parsed JSON string
            - model (str): the model name used
            - usage (dict): token usage stats
            - data (dict | None): parsed JSON if response_json_schema was provided
    """
    body = {
        "prompt": prompt,
        "messages": messages,
        "temperature": temperature,
    }
    if response_json_schema:
        body["response_json_schema"] = response_json_schema

    # ── Task-based model routing ──
    # When a hierarchical endpoint is pinned (project/agent → llm_models),
    # it wins over task-based routing: the pinned model_id is used verbatim
    # so the whole request answers with the selected model.
    resolved_messages = messages or [{"role": "user", "content": prompt}]
    if endpoint is not None:
        routed_model = endpoint.model_id
        logger.info(
            "call_llm: using pinned hierarchical endpoint model=%s base_url=%s",
            endpoint.model_id, endpoint.base_url,
        )
    else:
        from app.services.model_router import get_model_for_request

        routed_model = get_model_for_request(
            resolved_messages,
            tools_specified=False,
            explicit_task=task_type,
        )

    payload, built_messages, has_schema = build_llm_payload(body, routed_model)

    # ── Defense-in-depth: sanitize orphan assistant.tool_calls before send ──
    # Stripped messages are the root cause of "agent not responding" under
    # qwen3.6-27b: deepseek (catalog default for background calls) returns
    # HTTP 400 for orphan tool_calls -> record_failure -> circuit-breaker
    # opens -> all providers exhausted -> 502 with no detail.  See
    # _sanitize_tool_call_pairing docstring for the full failure path.
    sanitized_messages = _sanitize_tool_call_pairing(built_messages)
    if len(sanitized_messages) != len(built_messages) or any(
        (m.get("tool_calls") != o.get("tool_calls"))
        for m, o in zip(sanitized_messages, built_messages)
    ):
        logger.warning(
            "call_llm: sanitized messages (%d → %d) — orphan tool_calls stripped",
            len(built_messages), len(sanitized_messages),
        )
    payload["messages"] = sanitized_messages
    built_messages = sanitized_messages

    # ── Cache check (temperature=0 only) ──
    # Skipped for pinned endpoints: the cache key does not carry endpoint
    # identity, so two endpoints sharing a model_id could collide.
    from app.services.llm_cache import get_cached_response, set_cached_response

    if endpoint is None:
        cached = get_cached_response(
            built_messages, routed_model, temperature,
            schema=response_json_schema,
        )
        if cached is not None:
            _get_tracer().record_llm_call(
                model=routed_model,
                prompt_tokens=cached.get("usage", {}).get("prompt_tokens", 0),
                completion_tokens=cached.get("usage", {}).get("completion_tokens", 0),
                latency_ms=0,
            )
            return cached

    # ── P1-5: per-model tool-output cap (before pre-flight) ──
    # Apply the model's per-message tool-output cap to oversized tool
    # results.  Cheap, deterministic, no LLM call.  Runs before the
    # pre-flight so that the payload-vs-context check sees the post-cap
    # messages and can downgrade max_tokens or fall back instead of
    # raising 502.
    from app.services.compaction.pre_api_prune import smart_truncate
    built_messages = smart_truncate(built_messages, model=routed_model)
    payload["messages"] = built_messages

    # ── Pre-flight: estimate total payload vs. model's context window ──
    # The total LLM payload is (system prompt + messages + tools).  A user
    # on a small-context model (e.g. qwen3.6-27b, 65,536 tokens) can
    # overflow even with a short conversation if the system prompt +
    # data-source context alone is 60k+ tokens.  We estimate the payload
    # here and either:
    #   (a) clamp max_tokens down so the request still fits, OR
    #   (b) raise a clear, actionable 502 if the payload itself doesn't
    #       fit even with max_tokens=0.
    from app.services.compaction import get_context_window, estimate_messages_tokens
    _model_ctx = get_context_window(routed_model)
    _est_input_tokens = estimate_messages_tokens(built_messages)
    _tools_tokens = sum(
        len(json.dumps(t)) // 4 for t in (payload.get("tools") or [])
    )
    _total_est = _est_input_tokens + _tools_tokens
    _reserved_for_output = (
        payload.get("max_tokens")
        or temperature
        and int(getattr(settings, "LLM_MAX_TOKENS_HARD_CAP", 4096))
        or int(getattr(settings, "LLM_MAX_TOKENS_HARD_CAP", 4096))
    )
    # Simplify: just use the hard cap as the default upper bound for max_tokens
    _reserved_for_output = int(getattr(settings, "LLM_MAX_TOKENS_HARD_CAP", 4096))
    if _total_est + _reserved_for_output > _model_ctx:
        _headroom = max(0, _model_ctx - _total_est)
        if _headroom <= 0:
            # Payload itself exceeds the model's context window — no
            # amount of max_tokens reduction will help.  Surface a clear
            # error so the user can switch to a larger-context model.
            _msg = (
                f"Payload too large for model '{routed_model}': "
                f"~{_total_est} tokens (system + messages + tools) "
                f"vs. context_window={_model_ctx}. "
                f"Switch to a model with a larger context window "
                f"(e.g. deepseek-v4-flash, 128k) or reduce the bound "
                f"data-source / skill context."
            )
            logger.error("call_llm pre-flight: %s", _msg)
            raise HTTPException(
                status_code=502,
                detail=f"LLM request failed (payload too large for model): {_msg}",
            )
        # Otherwise: clamp max_tokens to fit
        original_max = payload.get("max_tokens") or _reserved_for_output
        payload["max_tokens"] = min(original_max, _headroom)
        logger.warning(
            "call_llm pre-flight: clamping max_tokens from %d to %d "
            "(payload %d + output ≤ context_window %d for model %s)",
            original_max, payload["max_tokens"],
            _total_est, _model_ctx, routed_model,
        )

    # ── P0-2 parallel tool calls capability injection ──
    # When the feature flag is on AND the routed model accepts the
    # field, declare ``parallel_tool_calls: True`` so the LLM may emit
    # multiple tool_calls in a single assistant message.  This is a
    # no-op (default-off) for the existing 220+ payload call sites that
    # still go through this function; each provider's own gating on
    # the field decides the actual behaviour.
    if (
        getattr(settings, "LLM_PARALLEL_TOOL_CALLS_ENABLED", False)
        and _supports_parallel_tool_calls(routed_model)
    ):
        payload["parallel_tool_calls"] = True

    # ── Health-based provider selection ──
    if endpoint is not None:
        # Pinned hierarchical endpoint: target it directly — no failover,
        # no health routing (the caller explicitly selected this model).
        providers = [
            LLMProvider(
                name="endpoint",
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
                model=endpoint.model_id,
            )
        ]
    else:
        providers = get_llm_providers()
        if getattr(settings, "LLM_HEALTH_ROUTING_ENABLED", False) and len(providers) > 1:
            chosen = select_provider(providers)
            # Reorder: chosen first, then the rest
            providers = [chosen] + [p for p in providers if p.name != chosen.name]

    last_error: Exception | None = None
    _llm_start = _time.monotonic()
    _llm_model = routed_model
    _llm_prompt_tokens = 0
    _llm_completion_tokens = 0
    _llm_error: str | None = None
    for provider in providers:
        if not is_healthy(provider.name):
            logger.debug("Provider '%s' is unhealthy — skipping", provider.name)
            continue
        request_payload = dict(payload)
        request_payload["model"] = provider.model
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    _provider_chat_url(provider),
                    headers=_provider_headers(provider),
                    json=request_payload,
                )
                resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            from app.services.api_error_classifier import classify_api_error
            from app.services.llm_retry import is_transient
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            ce = classify_api_error(e, status_code=status)
            last_error = e
            _llm_error = str(e)
            record_failure(provider.name, ce.reason.value)
            logger.warning(
                "call_llm: provider '%s' failed (%s, reason=%s): %s",
                provider.name, type(e).__name__, ce.reason.value, e,
            )
            if not is_transient(ce):
                if isinstance(e, httpx.HTTPStatusError):
                    raise HTTPException(
                        status_code=e.response.status_code,
                        detail=f"LLM API error: {e.response.text}",
                    ) from e
                raise HTTPException(
                    status_code=502, detail=f"LLM request failed: {e}",
                ) from e
            continue

        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        _llm_model = provider.model
        _llm_prompt_tokens = usage.get("prompt_tokens", 0)
        _llm_completion_tokens = usage.get("completion_tokens", 0)

        result = {
            "model": provider.model,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        if has_schema:
            try:
                parsed = json.loads(choice)
                if isinstance(parsed, dict):
                    result.update(parsed)
                    result["data"] = parsed
                else:
                    result["response"] = parsed
            except (json.JSONDecodeError, TypeError):
                result["response"] = choice
        else:
            result["response"] = choice

        latency_ms = (_time.monotonic() - _llm_start) * 1000
        record_success(provider.name, latency_ms)
        _get_tracer().record_llm_call(
            model=_llm_model,
            prompt_tokens=_llm_prompt_tokens,
            completion_tokens=_llm_completion_tokens,
            latency_ms=latency_ms,
        )

        # ── Cache result (temperature=0 only) ──
        if endpoint is None:
            set_cached_response(
                built_messages, provider.model, temperature,
                result, schema=response_json_schema,
            )

        return result

    # All providers failed — preserve the historical HTTPException surface
    _get_tracer().record_llm_call(
        model=_llm_model,
        prompt_tokens=_llm_prompt_tokens,
        completion_tokens=_llm_completion_tokens,
        latency_ms=(_time.monotonic() - _llm_start) * 1000,
        error=_llm_error,
    )
    if isinstance(last_error, httpx.HTTPStatusError):
        raise HTTPException(
            status_code=last_error.response.status_code,
            detail=f"LLM API error (all providers exhausted): {last_error.response.text}",
        )
    raise HTTPException(
        status_code=502,
        detail=f"LLM request failed (all providers exhausted): {last_error}",
    )


def embedding_url() -> str:
    """Return the full URL for the embeddings endpoint."""
    return f"{settings.OPENAI_BASE_URL.rstrip('/')}/embeddings"


def get_embedding(text: str, model: str | None = None) -> list[float] | None:
    """Return the embedding vector for ``text``, or ``None`` on any failure.

    Synchronous (uses ``httpx.Client``) so it can be called from the
    synchronous SQLAlchemy memory paths (``save_memory`` /
    ``search_memories`` / the backfill script).

    Returns ``None`` when:
      - ``text`` is empty,
      - no API key is configured,
      - the provider does not expose an ``/embeddings`` endpoint,
      - the request fails for any reason.

    Callers MUST treat ``None`` as "no embedding available" and fall back
    to a non-semantic path. This keeps memory recall working in dev/offline
    and with providers (e.g. DeepSeek) that lack an embeddings API.
    """
    if not text or not text.strip():
        return None
    if not settings.OPENAI_API_KEY:
        return None
    if not getattr(settings, "MEMORY_EMBEDDINGS_ENABLED", True):
        return None

    payload = {
        "model": model or settings.EMBEDDING_MODEL,
        "input": text[:8000],  # bound input length for safety
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(embedding_url(), headers=llm_headers(), json=payload)
            resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.debug("get_embedding failed (non-fatal): %s", e)
        return None
    try:
        data = resp.json()
        vec = data["data"][0]["embedding"]
        if isinstance(vec, list):
            return [float(x) for x in vec]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.debug("get_embedding parse failed (non-fatal): %s", e)
    return None


def chat_completion_json_sync(
    prompt: str,
    schema: dict | None = None,
    temperature: float = 0.0,
    endpoint: Optional[Any] = None,
) -> dict:
    """Synchronous JSON completion helper for small classifier-style calls.

    Used by planning_trigger and verifier sub-agent. Blocks the calling
    thread on httpx (sync client), so only call from opt-in paths where a
    short (~200ms) classify latency is acceptable. On any error, returns
    ``{}`` — callers must treat empty dict as "no verdict".

    ``endpoint`` (an ``LLMEndpoint`` from hierarchical LLM resolution) pins
    the call to a specific provider+model.  When ``None`` (default), the
    legacy provider-failover behavior applies.
    """
    body = {
        "prompt": prompt,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if schema:
        body["response_json_schema"] = schema
    payload, _, has_schema = build_llm_payload(body, endpoint.model_id if endpoint is not None else get_model())
    if endpoint is not None:
        providers = [
            LLMProvider(
                name="endpoint",
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
                model=endpoint.model_id,
            )
        ]
    else:
        providers = get_llm_providers()
    for provider in providers:
        request_payload = dict(payload)
        request_payload["model"] = provider.model
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    _provider_chat_url(provider),
                    headers=_provider_headers(provider),
                    json=request_payload,
                )
                resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.debug(
                "chat_completion_json_sync: provider '%s' failed: %s",
                provider.name, e,
            )
            continue
        try:
            data = resp.json()
            choice = data["choices"][0]["message"]["content"]
            if has_schema:
                return json.loads(choice) if isinstance(choice, str) else (choice or {})
            # No schema — still try to parse JSON; otherwise return {"response": choice}.
            try:
                return json.loads(choice)
            except (json.JSONDecodeError, TypeError):
                return {"response": choice}
        except Exception as e:
            logger.debug("chat_completion_json_sync parse failed: %s", e)
            continue
    return {}


def _clean_excerpt(text: str, limit: int = 200) -> str:
    """Collapse control chars and cap length so the text is safe for UI/logs."""
    if text is None:
        return ""
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or (31 < ord(ch) < 127) or ord(ch) > 127
    )
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def test_llm_endpoint(
    base_url: str,
    api_key: str | None,
    model: str,
    prompt: str = "ping",
    max_tokens: int = 8,
    timeout: float = 8.0,
) -> dict:
    """Sync, one-shot probe of an OpenAI-compatible chat endpoint.

    Returns ``{ok, latency_ms, status_code, response_text, error}``.
    ``response_text`` / ``error`` are bounded excerpts, safe to surface in
    UI or logs. Never includes the api_key.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}" if api_key else "",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        # Omit temperature so we honor each provider's default. Some models
        # (e.g. Moonshot kimi-k2.6) reject any temperature other than 1, which
        # would turn a valid key into a misleading HTTP 400 on the probe.
    }
    started = _time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
        latency_ms = int((_time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            try:
                snippet = _clean_excerpt(resp.text)
            except Exception:
                snippet = ""
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "status_code": resp.status_code,
                "response_text": None,
                "error": snippet or resp.reason_phrase or f"HTTP {resp.status_code}",
            }
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "status_code": resp.status_code,
                "response_text": None,
                "error": "Invalid JSON response",
            }
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "status_code": resp.status_code,
            "response_text": _clean_excerpt(str(content)),
            "error": None,
        }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "latency_ms": int((_time.monotonic() - started) * 1000),
            "status_code": None,
            "response_text": None,
            "error": f"Timeout after {int(timeout)}s",
        }
    except httpx.RequestError as e:
        return {
            "ok": False,
            "latency_ms": int((_time.monotonic() - started) * 1000),
            "status_code": None,
            "response_text": None,
            "error": f"Network error: {e.__class__.__name__}: {e}",
        }


async def stream_chat_completion(
    prompt: str,
    temperature: float = 0.7,
    endpoint: Optional[Any] = None,
) -> AsyncGenerator[str, None]:
    """Stream content deltas from the chat completions endpoint.

    A lightweight streaming variant for plain-text responses (no tool-call
    parsing). Used by the FSM FINALIZE response streaming so FSM-routed
    turns stream token-by-token like ReAct turns.

    ``endpoint`` (an ``LLMEndpoint`` from hierarchical LLM resolution) pins
    the stream to a specific provider+model.  When ``None`` (default), the
    legacy provider-failover behavior applies.

    Provider failover: the first provider to successfully OPEN the stream is
    used for the whole response — mid-stream failover is not possible once
    tokens have been emitted (the client would see garbled output), so a
    mid-stream error stops the stream rather than switching. On a
    connection-level failure (before any token), the next provider is tried.
    If no provider connects, the generator yields nothing and the caller
    should fall back to a blocking ``call_llm``.

    Health-based ordering: unhealthy providers are skipped; the healthiest
    (lowest-latency healthy) provider is tried first.
    """
    body = {"prompt": prompt, "messages": None, "temperature": temperature}
    payload, _, _ = build_llm_payload(body, endpoint.model_id if endpoint is not None else get_model())
    payload["stream"] = True

    if endpoint is not None:
        # Pinned hierarchical endpoint: stream directly from it — no
        # failover, no health routing (the caller explicitly selected it).
        providers = [
            LLMProvider(
                name="endpoint",
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
                model=endpoint.model_id,
            )
        ]
    else:
        providers = get_llm_providers()
        if getattr(settings, "LLM_HEALTH_ROUTING_ENABLED", False) and len(providers) > 1:
            chosen = select_provider(providers)
            providers = [chosen] + [p for p in providers if p.name != chosen.name]

    for provider in providers:
        if not is_healthy(provider.name):
            logger.debug("stream_chat_completion: provider '%s' unhealthy — skip", provider.name)
            continue
        request_payload = dict(payload)
        request_payload["model"] = provider.model
        started = False
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    _provider_chat_url(provider),
                    headers=_provider_headers(provider),
                    json=request_payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[len("data: "):]
                        if data_str.strip() == "[DONE]":
                            record_success(provider.name, 0)
                            return
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        content, _reasoning = extract_stream_parts(chunk)
                        if content:
                            started = True
                            yield content
            record_success(provider.name, 0)
            return  # streamed successfully from this provider
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            record_failure(provider.name, type(e).__name__)
            if started:
                logger.warning(
                    "stream_chat_completion: provider '%s' failed mid-stream: %s — stopping",
                    provider.name, e,
                )
                return
            logger.warning(
                "stream_chat_completion: provider '%s' failed to connect: %s — trying next",
                provider.name, e,
            )
            continue
    return  # no provider connected; caller falls back
