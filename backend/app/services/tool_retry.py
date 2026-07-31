"""Tool retry & self-healing helpers.

Wraps a tool handler so transient infrastructure failures (network timeouts,
5xx responses, connection resets) are retried with exponential backoff
before being surfaced to the LLM. Permanent failures (validation errors,
permission denials, missing_config) are NOT retried — they are returned
immediately so the LLM can either fix the arguments or escalate to the user.

This module is intentionally lightweight: no external dependencies beyond
asyncio, and no shared state, so it can be used from any tool-dispatch
context (sync or async, with or without a DB session).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Errors that indicate a transient infrastructure problem and are worth
# retrying. These are conservative — we'd rather escalate a transient
# error than accidentally retry a permanent one.
RETRYABLE_ERRORS: frozenset[type[BaseException]] = frozenset({
    asyncio.TimeoutError,
    ConnectionError,
    TimeoutError,
    ConnectionResetError,
    ConnectionAbortedError,
})


def is_retryable(error: BaseException) -> bool:
    """Classify an exception as transient (worth retrying) or permanent.

    Delegates to the structured API error classifier (P1) when possible,
    falling back to the legacy exception-type + string matching for
    non-API errors (file I/O, etc.).

    Args:
        error: The exception that was raised by the tool handler.

    Returns:
        True if the error looks transient, False otherwise.
    """
    # P1: use structured error classifier for API/network errors
    try:
        from app.services.api_error_classifier import classify_api_error
        ce = classify_api_error(error)
        if ce.reason.value != "unknown":
            return ce.retryable
    except Exception:
        pass  # classifier unavailable — fall back to legacy

    # Legacy: exception-type based classification for non-API errors
    if isinstance(error, tuple(RETRYABLE_ERRORS)):
        return True
    # Some third-party libs raise OSError subclasses (e.g. httpx.ConnectError).
    # Treat "network-ish" OSError subclasses as retryable but NOT pure FileNotFoundError.
    if isinstance(error, OSError) and error.__class__ is not OSError:
        name = error.__class__.__name__.lower()
        if any(token in name for token in ("connect", "timeout", "reset", "refused", "unavailable")):
            return True
    return False


def _failure_dict(error: BaseException, tool_name: str | None = None) -> dict[str, Any]:
    """Build the standard {"success": False, "error": ...} return shape."""
    message = f"{type(error).__name__}: {error}"
    if tool_name:
        message = f"Tool '{tool_name}' failed: {message}"
    return {"success": False, "error": message, "error_type": type(error).__name__}


async def retry_with_backoff(
    handler: Callable[..., Awaitable[dict] | dict],
    arguments: dict,
    db: Any,
    user_id: Optional[str],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    context: dict | None = None,
    tool_name: str | None = None,
) -> dict:
    """Execute ``handler`` with exponential-backoff retry on transient errors.

    The handler may be sync or async. Async handlers are awaited directly; sync
    handlers are invoked via ``asyncio.to_thread`` so the event loop is not
    blocked. The handler is expected to return a dict; if it raises an
    exception, the exception is classified via :func:`is_retryable` and either
    retried (with ``base_delay * 2**attempt`` sleep) or returned as a
    ``{"success": False, ...}`` dict.

    Args:
        handler: The tool handler callable.
        arguments: Tool arguments forwarded to the handler.
        db: Optional database session forwarded to the handler.
        user_id: Optional user ID forwarded to the handler.
        max_retries: Maximum number of attempts (including the first). Default 3.
        base_delay: Initial sleep in seconds. Doubles on each retry. Default 1.0.
        context: Optional runtime context forwarded to the handler.
        tool_name: Optional tool name used in error messages and logging.

    Returns:
        The handler's return dict on success, or a failure dict on permanent
        error or retry exhaustion.
    """
    attempt = 0
    last_error: BaseException | None = None

    while attempt < max_retries:
        try:
            if asyncio.iscoroutinefunction(handler):
                return await handler(arguments, db, user_id, context=context or {})
            # Sync handler — run in a thread to keep the event loop responsive.
            return await asyncio.to_thread(handler, arguments, db, user_id)
        except Exception as e:  # noqa: BLE001 — we re-raise only on non-retryable below
            last_error = e
            if not is_retryable(e):
                logger.info(
                    "Tool '%s' permanent error (no retry): %s: %s",
                    tool_name or "?", type(e).__name__, e,
                )
                _fd = _failure_dict(e, tool_name)
                _fd["failure_kind"] = "permanent"
                return _fd

            attempt += 1
            if attempt >= max_retries:
                logger.warning(
                    "Tool '%s' exhausted %d retries on transient error: %s: %s",
                    tool_name or "?", max_retries, type(e).__name__, e,
                )
                break

            delay = base_delay * (2 ** (attempt - 1))
            logger.info(
                "Tool '%s' transient error (attempt %d/%d), retrying in %.1fs: %s: %s",
                tool_name or "?", attempt, max_retries, delay, type(e).__name__, e,
            )
            await asyncio.sleep(delay)

    # Retry exhaustion on a transient error.
    assert last_error is not None  # for type checkers
    _fd = _failure_dict(last_error, tool_name)
    _fd["failure_kind"] = "transient_exhausted"
    return _fd


async def reformulate_tool_args(
    tool_name: str,
    arguments: dict,
    error: str,
    llm_fn: Optional[Callable[[str], Awaitable[str]]] = None,
) -> dict:
    """Ask the LLM to propose corrected arguments after a permanent tool failure.

    Returns a (possibly) corrected arguments dict. On any failure — no LLM
    available, unparseable response, or the LLM returning nothing useful —
    the original ``arguments`` are returned unchanged. Never raises.

    Callers should compare the result to ``arguments`` and only re-execute
    the tool when they differ (otherwise the LLM had nothing to fix).

    Args:
        tool_name: The name of the tool that failed.
        arguments: The arguments that caused the failure.
        error: The error message returned by the tool.
        llm_fn: Optional async callable ``(prompt: str) -> str``. If None,
            the shared sync ``chat_completion_json_sync`` is invoked via a
            worker thread so the event loop is not blocked.

    Returns:
        A dict of arguments — corrected when the LLM proposed a fix, else
        the original ``arguments``.
    """
    import json as _json

    prompt = (
        "A tool call failed. Propose corrected arguments as a JSON object.\n\n"
        f"Tool: {tool_name}\n"
        f"Failed arguments: {_json.dumps(arguments, ensure_ascii=False)}\n"
        f"Error: {error}\n\n"
        "Rules:\n"
        "- Only change what is necessary to fix the error.\n"
        "- Do NOT add or remove keys unrelated to the failure.\n"
        "- If the error cannot be fixed by changing arguments (e.g. "
        "permission denied, resource missing, unsupported operation), "
        "return the original arguments unchanged.\n\n"
        "Respond with ONLY the JSON object."
    )

    try:
        if llm_fn is not None:
            raw = await llm_fn(prompt)
        else:
            from app.services.llm_service import chat_completion_json_sync
            import asyncio as _aio
            raw = await _aio.to_thread(chat_completion_json_sync, prompt)
    except Exception as e:
        logger.debug("reformulate_tool_args LLM call failed: %s", e)
        return arguments

    corrected = _parse_args_response(raw)
    if not isinstance(corrected, dict) or not corrected:
        return arguments
    return corrected


def _parse_args_response(raw: Any) -> dict | None:
    """Best-effort extraction of a JSON args dict from an LLM response.

    ``chat_completion_json_sync`` returns either a parsed dict (when the
    model emitted valid JSON) or ``{"response": <text>}``. This helper
    accepts both shapes, strips code fences, and parses. Returns None when
    no dict can be recovered.
    """
    import json as _json

    if isinstance(raw, dict):
        # Already-parsed JSON object (the happy path).
        if "response" in raw and len(raw) == 1:
            return _parse_args_response(raw["response"])
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if "```json" in text:
        text = text.split("```json", 1)[-1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[-1].split("```", 1)[0]
    try:
        parsed = _json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
