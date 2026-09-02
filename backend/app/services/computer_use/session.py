"""Shared per-conversation agent-browser session store.

One Chromium/CDP session per conversation, shared by the ``agent_browser``
and ``computer_use`` tools so a single chat never spawns more than one
browser. Records carry ``url``/``title`` (navigation state) plus an action
budget used by ``computer_use`` (``steps_used`` / ``COMPUTER_USE_MAX_STEPS``).
Idle sessions expire after a TTL; ``close`` frees the CLI session.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Filesystem layout — shared by both tools (screenshots / extract dumps).
BROWSER_DIR = Path(os.environ.get("ZHANLU_BROWSER_DIR", "/tmp/zhanlu_browser"))
try:
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    BROWSER_DIR = Path("/var/tmp/zhanlu_browser")
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_sessions: Dict[str, Dict[str, Any]] = {}


def get_session(conversation_id: str, ttl_seconds: int = 1800) -> Dict[str, Any]:
    """Return the session record for a conversation, creating it if needed.

    An expired record is recycled: the old CLI session id is forgotten and
    a fresh id with a reset action budget is allocated. Touches
    ``last_seen`` so shared sessions stay warm across both tools.
    """
    now = time.monotonic()
    with _lock:
        existing = _sessions.get(conversation_id)
        if existing is not None and now - existing.get("last_seen", 0) <= ttl_seconds:
            existing["last_seen"] = now
            return existing
        if existing is not None:
            _sessions.pop(conversation_id, None)
        record: Dict[str, Any] = {
            "session_id": f"zhanlu-{conversation_id}-{uuid.uuid4().hex[:8]}",
            "url": None,
            "title": None,
            "steps_used": 0,
            "last_seen": now,
        }
        _sessions[conversation_id] = record
        return record


def cleanup_session(conversation_id: str) -> None:
    """Forget a session and best-effort close the underlying CLI session."""
    with _lock:
        sess = _sessions.pop(conversation_id, None)
    if not sess:
        return
    try:
        subprocess.run(
            ["agent-browser", "close", "--session", sess["session_id"]],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover — best-effort cleanup
        logger.debug("agent-browser close failed for %s: %s", conversation_id, exc)


def register_action(conversation_id: str, amount: int = 1) -> int:
    """Charge an action against the conversation's budget; returns steps used."""
    with _lock:
        sess = _sessions.get(conversation_id)
        if sess is None:
            return 0
        sess["steps_used"] += amount
        sess["last_seen"] = time.monotonic()
        return sess["steps_used"]


def is_exhausted(conversation_id: str, max_steps: int) -> bool:
    """True when the conversation has used up its action budget."""
    if max_steps <= 0:
        return False
    with _lock:
        sess = _sessions.get(conversation_id)
        if sess is None:
            return False
        return sess["steps_used"] >= max_steps
