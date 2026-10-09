"""Run control — user-requested mid-turn cancellation (IBM best practice
"interruption", G1).

A running agent turn registers itself per conversation; a cancel request
flips a flag that the tool loop checks at the top of every iteration and
that gates the expensive post-loop synthesis. On cancel the loop breaks,
partial content is preserved (the normal finalize path runs), and the
done event carries ``"status": "cancelled"``.

In-process only (single-backend deployments) — a multi-backend deployment
would move the flag to Redis. Never raises.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_ACTIVE_RUNS: set[str] = set()
_CANCELLED: set[str] = set()
_LOCK = threading.Lock()


def register_run(conversation_id: str) -> None:
    """Mark a run active; clear any stale cancel flag from a prior turn."""
    if not conversation_id:
        return
    with _LOCK:
        _ACTIVE_RUNS.add(conversation_id)
        _CANCELLED.discard(conversation_id)


def unregister_run(conversation_id: str) -> None:
    if not conversation_id:
        return
    with _LOCK:
        _ACTIVE_RUNS.discard(conversation_id)
        _CANCELLED.discard(conversation_id)


def request_cancel(conversation_id: str) -> dict:
    """User asked to stop the running turn. Returns status info."""
    with _LOCK:
        active = conversation_id in _ACTIVE_RUNS
        if active:
            _CANCELLED.add(conversation_id)
    logger.info("run_control: cancel requested (conv=%s, active=%s)", conversation_id, active)
    return {"ok": True, "cancelling": active}


def is_cancelled(conversation_id: str) -> bool:
    if not conversation_id:
        return False
    with _LOCK:
        return conversation_id in _CANCELLED


def active_runs() -> int:
    with _LOCK:
        return len(_ACTIVE_RUNS)


__all__ = [
    "register_run",
    "unregister_run",
    "request_cancel",
    "is_cancelled",
    "active_runs",
]
