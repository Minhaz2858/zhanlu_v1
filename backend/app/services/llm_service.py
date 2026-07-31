"""Shared LLM calling utility — extracted from integrations.py for reuse.

Both integrations.py (InvokeLLM, InvokeLLMStream) and agents.py (add_message)
use these helpers to call the configured OpenAI-compatible LLM (DeepSeek).
"""

import json
import logging
from dataclasses import dataclass
from typing import AsyncGenerator

import httpx
from fastapi import HTTPException

from app.config import settings
from app.services.tracing import get_tracer as _get_tracer

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
        "temperature": temperature,
    }
    if json_schema:
        payload["response_format"] = {"type": "json_object"}

    return payload, messages, bool(json_schema)


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
) -> dict:
    """Call the configured LLM and return a parsed response dict.

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

    payload, built_messages, has_schema = build_llm_payload(body, get_model())

    providers = get_llm_providers()
    last_error: Exception | None = None
    _llm_start = _time.monotonic()
    _llm_model = get_model()
    _llm_prompt_tokens = 0
    _llm_completion_tokens = 0
    _llm_error: str | None = None
    for provider in providers:
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
            logger.warning(
                "call_llm: provider '%s' failed (%s, reason=%s): %s",
                provider.name, type(e).__name__, ce.reason.value, e,
            )
            if not is_transient(ce):
                # Permanent failure (bad request, auth, billing, …): trying
                # the next provider would reproduce the same error and mask
                # the real one. Fail fast with the historical HTTPException
                # surface so existing callers keep working.
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

        _get_tracer().record_llm_call(
            model=_llm_model,
            prompt_tokens=_llm_prompt_tokens,
            completion_tokens=_llm_completion_tokens,
            latency_ms=(_time.monotonic() - _llm_start) * 1000,
        )
        return result

    # All providers failed — preserve the historical HTTPException surface so
    # existing callers (which catch HTTPException) keep working.
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
) -> dict:
    """Synchronous JSON completion helper for small classifier-style calls.

    Used by planning_trigger and verifier sub-agent. Blocks the calling
    thread on httpx (sync client), so only call from opt-in paths where a
    short (~200ms) classify latency is acceptable. On any error, returns
    ``{}`` — callers must treat empty dict as "no verdict".
    """
    body = {
        "prompt": prompt,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if schema:
        body["response_json_schema"] = schema
    payload, _, has_schema = build_llm_payload(body, get_model())
    for provider in get_llm_providers():
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


async def stream_chat_completion(
    prompt: str,
    temperature: float = 0.7,
) -> AsyncGenerator[str, None]:
    """Stream content deltas from the chat completions endpoint.

    A lightweight streaming variant for plain-text responses (no tool-call
    parsing). Used by the FSM FINALIZE response streaming so FSM-routed
    turns stream token-by-token like ReAct turns.

    Provider failover: the first provider to successfully OPEN the stream is
    used for the whole response — mid-stream failover is not possible once
    tokens have been emitted (the client would see garbled output), so a
    mid-stream error stops the stream rather than switching. On a
    connection-level failure (before any token), the next provider is tried.
    If no provider connects, the generator yields nothing and the caller
    should fall back to a blocking ``call_llm``.
    """
    body = {"prompt": prompt, "messages": None, "temperature": temperature}
    payload, _, _ = build_llm_payload(body, get_model())
    payload["stream"] = True

    for provider in get_llm_providers():
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
                            return
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        content, _reasoning = extract_stream_parts(chunk)
                        if content:
                            started = True
                            yield content
            return  # streamed successfully from this provider
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
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
