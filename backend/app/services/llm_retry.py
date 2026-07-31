"""Transient-retry primitives for the LLM call paths.

Spec: automation quality program Phase 1 (§4.1). Centralizes what used to
be ad-hoc: status-code-first classification (via ``api_error_classifier``),
Retry-After honoring, exponential backoff with full jitter, and a bounded
async retry loop. UNKNOWN errors are fail-safe (no retry storms).
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Optional, TypeVar

from app.services.api_error_classifier import (
    ClassifiedError,
    FailoverReason,
    classify_api_error,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_REASONS: frozenset = frozenset({
    FailoverReason.rate_limit,
    FailoverReason.upstream_rate_limit,
    FailoverReason.overloaded,
    FailoverReason.server_error,
    FailoverReason.timeout,
    FailoverReason.connection_error,
})

_MAX_ATTEMPTS_CAP = 3
_MAX_DELAY_SECONDS = 30.0


def is_transient(ce: ClassifiedError) -> bool:
    """True when the classified error is worth a backoff retry."""
    return ce.retryable and ce.reason in TRANSIENT_REASONS


def extract_status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Parse the Retry-After response header (delta-seconds or HTTP-date)."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(raw)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def max_attempts_for(ce: ClassifiedError) -> int:
    """Attempt budget: the classifier's per-error max_retries, capped at 3."""
    return max(1, min(_MAX_ATTEMPTS_CAP, ce.max_retries or 1))


def next_backoff(attempt: int, retry_after: Optional[float] = None) -> float:
    """Delay before retry ``attempt`` (0-based). Retry-After wins; otherwise
    exponential ``min(30, 2**attempt)`` with full jitter."""
    if retry_after is not None:
        return min(_MAX_DELAY_SECONDS, retry_after + random.uniform(0.0, 1.0))
    base = min(_MAX_DELAY_SECONDS, 2.0 ** attempt)
    return random.uniform(0.0, base)


async def call_with_transient_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    on_retry: Optional[Callable[[int, float, ClassifiedError], Awaitable[None]]] = None,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``factory()`` retrying classified-transient errors with backoff.

    ``factory`` is invoked fresh per attempt. ``on_retry(next_attempt,
    delay, ce)`` fires before each backoff sleep (``next_attempt`` is the
    1-based number of the upcoming attempt). Non-transient errors raise
    immediately; transient errors raise once the budget is exhausted.
    """
    attempt = 1
    while True:
        try:
            return await factory()
        except Exception as e:
            ce = classify_api_error(e, status_code=extract_status_code(e))
            if not is_transient(ce) or attempt >= max_attempts_for(ce):
                raise
            delay = next_backoff(attempt - 1, retry_after_seconds(e))
            logger.warning(
                "llm_retry: transient %s on attempt %d; retrying in %.1fs",
                ce.reason.value, attempt, delay,
            )
            if on_retry is not None:
                await on_retry(attempt + 1, delay, ce)
            await _sleep(delay)
            attempt += 1
