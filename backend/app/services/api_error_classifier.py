"""Structured API error classification for the LLM call path.

Centralizes error classification that was previously scattered across
``tool_retry.py`` (string matching) and ``agents.py`` (inline "prompt too
long" checks). Each error is classified into a :class:`FailoverReason` enum
value, and a :class:`ClassifiedError` carries actionable recovery hints:

- ``retryable``: should we retry with backoff?
- ``should_compress``: should we run compaction before retrying?
- ``should_fallback``: should we switch to a fallback model/provider?
- ``should_rotate_credential``: should we try a different API key?

Inspired by Hermes' ``agent/error_classifier.py``, adapted for Zhanlu's
DeepSeek + OpenAI-compatible provider stack (fewer auth/credential
scenarios, more context-overflow scenarios).
"""
from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class FailoverReason(enum.Enum):
    """Why an API call failed -- determines recovery strategy."""
    unknown = "unknown"
    # Transient -- retry with backoff
    rate_limit = "rate_limit"
    upstream_rate_limit = "upstream_rate_limit"
    overloaded = "overloaded"
    server_error = "server_error"
    timeout = "timeout"
    connection_error = "connection_error"
    # Context -- needs compaction
    context_overflow = "context_overflow"
    payload_too_large = "payload_too_large"
    # Permanent -- don't retry as-is
    auth = "auth"
    billing = "billing"
    model_not_found = "model_not_found"
    content_policy_blocked = "content_policy_blocked"
    provider_policy_blocked = "provider_policy_blocked"
    format_error = "format_error"
    bad_request = "bad_request"


@dataclass(frozen=True)
class ClassifiedError:
    """An error classified with recovery hints.

    Attributes:
        reason: The classified FailoverReason.
        retryable: Whether a retry with backoff might succeed.
        should_compress: Whether to run compaction before retrying.
        should_fallback: Whether to switch to a fallback model/provider.
        should_rotate_credential: Whether to try a different API key.
        max_retries: Suggested max retries for this error type.
        message: Human-readable description.
        original: The original exception or response dict (for debugging).
    """
    reason: FailoverReason
    retryable: bool = False
    should_compress: bool = False
    should_fallback: bool = False
    should_rotate_credential: bool = False
    max_retries: int = 0
    message: str = ""
    original: Any = None


# -- String markers for classification (case-insensitive substring match) --

_RATE_LIMIT_MARKERS = (
    "rate limit", "rate_limit", "too many requests", "429",
    "requests per minute", "quota exceeded", "throttle",
)
_OVERLOADED_MARKERS = (
    "overloaded", "service unavailable", "503", "server is overloaded",
    "capacity", "temporarily unavailable",
)
_SERVER_ERROR_MARKERS = (
    "500", "internal server error", "bad gateway", "502",
    "gateway timeout", "504", "server error",
)
_TIMEOUT_MARKERS = (
    "timeout", "timed out", "deadline exceeded", "connection timed out",
)
_CONNECTION_MARKERS = (
    "connection error", "connection reset", "connection refused",
    "connection aborted", "eof occurred", "broken pipe",
    "network is unreachable", "ssl", "certifi",
)
_CONTEXT_OVERFLOW_MARKERS = (
    "context length", "context window", "maximum context", "too long",
    "prompt too long", "tokens exceeded", "max tokens",
    "reduce the length", "context_length_exceeded",
)
_AUTH_MARKERS = (
    "401", "unauthorized", "invalid api key", "authentication",
    "invalid_api_key", "incorrect api key",
)
_BILLING_MARKERS = (
    "402", "payment required", "billing", "quota exceeded",
    "insufficient balance", "insufficient funds", "insufficient quota",
    "insufficient credit", "credit limit", "plan limit", "subscription",
)

# 2026-08-29: "insufficient tool messages following tool_calls" (DeepSeek/
# OpenAI 400 when a persisted assistant tool_call lost its tool response)
# was previously swallowed by _BILLING_MARKERS via the generic word
# "insufficient" — users saw "Billing issue. Check account quota." while
# the real problem was a corrupted conversation history. Classify it
# explicitly so the user gets an actionable message instead.
_DANGLING_TOOL_CALL_MARKERS = (
    "tool messages following tool_calls",
    "insufficient tool messages",
    "must be followed by tool messages",
    "each 'tool_call_id'",
)
_MODEL_NOT_FOUND_MARKERS = (
    "model not found", "does not exist", "404", "not_found",
    "invalid model",
)
_CONTENT_POLICY_MARKERS = (
    "content policy", "content_filter", "safety", "blocked",
    "flagged", "violation", "inappropriate",
)
_FORMAT_ERROR_MARKERS = (
    "invalid_request_error", "bad request", "400", "malformed",
    "invalid json", "parse error", "unexpected token",
)


def _match_any(text: str, markers: tuple[str, ...]) -> bool:
    text_lower = text.lower()
    return any(m in text_lower for m in markers)


def _extract_error_text(exc: BaseException | dict | str | None) -> str:
    """Extract a searchable error string from an exception, dict, or string."""
    if exc is None:
        return ""
    if isinstance(exc, str):
        return exc
    if isinstance(exc, BaseException):
        # Check for common HTTP exception attributes
        for attr in ("message", "body", "response", "args"):
            val = getattr(exc, attr, None)
            if val is not None:
                if isinstance(val, (dict, list)):
                    try:
                        return json.dumps(val, ensure_ascii=False, default=str)
                    except (TypeError, ValueError):
                        pass
                if isinstance(val, str):
                    return val
                if isinstance(val, BaseException):
                    return str(val)
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, dict):
        # Try common error dict shapes
        for key in ("message", "error", "detail", "reason"):
            if key in exc:
                val = exc[key]
                if isinstance(val, str):
                    return val
                if isinstance(val, dict):
                    return json.dumps(val, ensure_ascii=False, default=str)
        return json.dumps(exc, ensure_ascii=False, default=str)
    return str(exc)


def classify_api_error(
    error: BaseException | dict | str | None,
    *,
    status_code: int | None = None,
) -> ClassifiedError:
    """Classify an API error into a :class:`ClassifiedError` with recovery hints.

    Args:
        error: The exception, error dict, or error string from the API call.
        status_code: Optional HTTP status code (takes precedence over string
            matching for ambiguous cases).

    Returns:
        A ClassifiedError with actionable recovery hints.

    Examples:
        >>> classify_api_error(Exception("rate limit exceeded"))
        ClassifiedError(reason=FailoverReason.rate_limit, retryable=True, ...)
        >>> classify_api_error({"error": "context_length_exceeded"}, status_code=400)
        ClassifiedError(reason=FailoverReason.context_overflow, should_compress=True, ...)
    """
    text = _extract_error_text(error)
    code = status_code

    # If no status code in args, try to extract from exception attributes
    if code is None and isinstance(error, BaseException):
        for attr in ("status_code", "status", "statusCode"):
            val = getattr(error, attr, None)
            if isinstance(val, int):
                code = val
                break

    # -- Status code based classification (most reliable) --
    if code is not None:
        if code == 429:
            return ClassifiedError(
                reason=FailoverReason.rate_limit, retryable=True,
                max_retries=3, message="Rate limited (429). Back off and retry.",
                original=error,
            )
        if code in (500, 502, 503, 504):
            reason = FailoverReason.overloaded if code == 503 else FailoverReason.server_error
            return ClassifiedError(
                reason=reason, retryable=True,
                max_retries=2, message=f"Server error ({code}). Retry with backoff.",
                original=error,
            )
        if code == 401:
            return ClassifiedError(
                reason=FailoverReason.auth, retryable=False,
                message="Authentication failed (401). Check API key.",
                original=error,
            )
        if code == 402:
            return ClassifiedError(
                reason=FailoverReason.billing, retryable=False,
                message="Billing issue (402). Check account quota.",
                original=error,
            )
        if code == 404:
            return ClassifiedError(
                reason=FailoverReason.model_not_found, retryable=False,
                should_fallback=True,
                message="Model not found (404). Try a fallback model.",
                original=error,
            )
        if code == 400:
            # 400 is ambiguous -- could be context overflow or format error
            if _match_any(text, _CONTEXT_OVERFLOW_MARKERS):
                return ClassifiedError(
                    reason=FailoverReason.context_overflow, retryable=True,
                    should_compress=True, max_retries=1,
                    message="Context overflow (400). Compress and retry.",
                    original=error,
                )
            if _match_any(text, _DANGLING_TOOL_CALL_MARKERS):
                return ClassifiedError(
                    reason=FailoverReason.format_error, retryable=False,
                    message=(
                        "Conversation history is inconsistent (a tool call is "
                        "missing its result). Please start a new chat."
                    ),
                    original=error,
                )
            if _match_any(text, _CONTENT_POLICY_MARKERS):
                return ClassifiedError(
                    reason=FailoverReason.content_policy_blocked, retryable=False,
                    message="Content policy blocked (400).",
                    original=error,
                )
            return ClassifiedError(
                reason=FailoverReason.bad_request, retryable=False,
                message="Bad request (400). Check request format.",
                original=error,
            )

    # -- String-based classification (fallback when no status code) --
    if _match_any(text, _RATE_LIMIT_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.rate_limit, retryable=True,
            max_retries=3, message="Rate limited. Back off and retry.",
            original=error,
        )
    if _match_any(text, _OVERLOADED_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.overloaded, retryable=True,
            max_retries=2, message="Server overloaded. Retry with backoff.",
            original=error,
        )
    if _match_any(text, _CONTEXT_OVERFLOW_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.context_overflow, retryable=True,
            should_compress=True, max_retries=1,
            message="Context overflow. Compress and retry.",
            original=error,
        )
    if _match_any(text, _DANGLING_TOOL_CALL_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.format_error, retryable=False,
            message=(
                "Conversation history is inconsistent (a tool call is "
                "missing its result). Please start a new chat."
            ),
            original=error,
        )
    if _match_any(text, _SERVER_ERROR_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.server_error, retryable=True,
            max_retries=2, message="Server error. Retry with backoff.",
            original=error,
        )
    if _match_any(text, _TIMEOUT_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.timeout, retryable=True,
            max_retries=2, message="Request timed out. Retry with backoff.",
            original=error,
        )
    if _match_any(text, _CONNECTION_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.connection_error, retryable=True,
            max_retries=2, message="Connection error. Retry with backoff.",
            original=error,
        )
    if _match_any(text, _AUTH_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.auth, retryable=False,
            message="Authentication failed. Check API key.",
            original=error,
        )
    if _match_any(text, _BILLING_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.billing, retryable=False,
            message="Billing issue. Check account quota.",
            original=error,
        )
    if _match_any(text, _MODEL_NOT_FOUND_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.model_not_found, retryable=False,
            should_fallback=True,
            message="Model not found. Try a fallback model.",
            original=error,
        )
    if _match_any(text, _CONTENT_POLICY_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.content_policy_blocked, retryable=False,
            message="Content policy blocked.",
            original=error,
        )
    if _match_any(text, _FORMAT_ERROR_MARKERS):
        return ClassifiedError(
            reason=FailoverReason.format_error, retryable=False,
            message="Format error. Check request format.",
            original=error,
        )

    # -- Fallback: unknown error, conservatively retryable once --
    return ClassifiedError(
        reason=FailoverReason.unknown, retryable=True,
        max_retries=1, message=f"Unknown error: {text[:200]}",
        original=error,
    )


__all__ = [
    "FailoverReason",
    "ClassifiedError",
    "classify_api_error",
]
