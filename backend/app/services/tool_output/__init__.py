"""Tool output management — adapted from OpenHarness.

Large tool outputs are offloaded to tool_artifacts/ directory,
with only a preview kept in the conversation context.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 8000
PREVIEW_CHARS = 500
ARTIFACTS_DIR = "tool_artifacts"


class ToolOutputManager:
    """Manages large tool outputs by offloading to disk.

    When a tool result exceeds MAX_OUTPUT_CHARS, the full output is
    written to disk under tool_artifacts/ and only a preview (first
    PREVIEW_CHARS) is returned inline.
    """

    def __init__(self, artifacts_dir: str = ""):
        self.artifacts_dir = Path(artifacts_dir or ARTIFACTS_DIR)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _artifact_path(self, tool_name: str, content_hash: str) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_tool = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_name)
        return self.artifacts_dir / f"{safe_tool}_{ts}_{content_hash[:8]}.txt"

    def offload_if_large(self, tool_name: str, content: str) -> dict:
        """Return either the full content (when small) or {preview, artifact_ref}."""
        if len(content) <= MAX_OUTPUT_CHARS:
            return {"inline": True, "content": content}
        content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        path = self._artifact_path(tool_name, content_hash)
        try:
            path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to offload tool output to %s: %s", path, exc)
            return {"inline": False, "truncated": content[:MAX_OUTPUT_CHARS],
                    "error": f"offload_failed: {exc}"}
        preview = content[:PREVIEW_CHARS]
        return {
            "inline": False,
            "preview": preview,
            "artifact_ref": str(path),
            "full_length": len(content),
            "preview_length": PREVIEW_CHARS,
            "content_hash": content_hash,
        }

    def get_artifact(self, ref: str) -> str | None:
        """Read a previously offloaded artifact by reference path."""
        try:
            p = Path(ref)
            if not p.exists():
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to read artifact %s: %s", ref, exc)
            return None


_manager: ToolOutputManager | None = None


def get_tool_output_manager() -> ToolOutputManager:
    global _manager
    if _manager is None:
        _manager = ToolOutputManager()
    return _manager


# Per-conversation UI event queue (added in Phase 1)
import threading
from collections import defaultdict, deque

_lock = threading.Lock()
_queues: dict = defaultdict(deque)
_MAX_PER_QUEUE = 100


def push_event(conversation_id: str, event: dict) -> None:
    with _lock:
        q = _queues[conversation_id]
        q.append(event)
        while len(q) > _MAX_PER_QUEUE:
            q.popleft()


def drain_events(conversation_id: str) -> list:
    with _lock:
        q = _queues.get(conversation_id)
        if not q:
            return []
        out = list(q)
        q.clear()
        return out


def peek_events(conversation_id: str) -> list:
    with _lock:
        return list(_queues.get(conversation_id, ()))


__all__ = [
    "MAX_OUTPUT_CHARS",
    "PREVIEW_CHARS",
    "ARTIFACTS_DIR",
    "ToolOutputManager",
    "get_tool_output_manager",
    "push_event",
    "drain_events",
    "peek_events",
]
