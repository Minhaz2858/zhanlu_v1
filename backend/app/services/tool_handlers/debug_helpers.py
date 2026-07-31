"""Debug helper for tool calls — captures JSON request/response to a per-call log.

Used by tools that opt in (e.g. web_tools' WEB_TOOLS_DEBUG env var) to
record the full payload/response for offline analysis. Logs go to
``./logs/<session_id>_<call_uuid>.json``.

Not registered as a tool — pure helper, imported by other tool modules.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.services.tool_handlers.tool_backend_helpers import is_truthy_value

logger = logging.getLogger(__name__)


class DebugSession:
    """Per-call debug capture. Writes a single JSON file when closed.

    Usage:
        with DebugSession("web_search", args={"query": "x"}) as ds:
            ds.record_response({"results": [...]})
    """

    def __init__(self, tool_name: str, args: Optional[dict] = None,
                 session_id: Optional[str] = None):
        self.tool_name = tool_name
        self.args = args or {}
        self.session_id = session_id or os.environ.get("ZHANLU_SESSION_ID", "anon")
        self.call_id = uuid.uuid4().hex[:8]
        self.started_at = time.time()
        self._response: Any = None
        self._metadata: dict = {}

    def record_response(self, response: Any) -> None:
        self._response = response

    def add_metadata(self, **kwargs: Any) -> None:
        self._metadata.update(kwargs)

    def _build_path(self) -> Path:
        log_dir = Path(os.environ.get("ZHANLU_DEBUG_LOG_DIR", "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = int(self.started_at * 1000)
        return log_dir / f"{self.tool_name}_{self.session_id}_{ts}_{self.call_id}.json"

    def write(self) -> Optional[Path]:
        """Flush the captured payload to disk. Returns the path or None on failure."""
        try:
            payload = {
                "tool_name": self.tool_name,
                "session_id": self.session_id,
                "call_id": self.call_id,
                "started_at": self.started_at,
                "duration_s": time.time() - self.started_at,
                "args": self.args,
                "response": self._response,
                "metadata": self._metadata,
            }
            path = self._build_path()
            path.write_text(
                json.dumps(payload, ensure_ascii=False, default=str, indent=2),
                encoding="utf-8",
            )
            return path
        except Exception as exc:
            logger.debug("DebugSession.write failed: %s", exc)
            return None

    # Context-manager support
    def __enter__(self) -> "DebugSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.add_metadata(error=f"{type(exc).__name__}: {exc}")
        self.write()


def debug_enabled(env_var: str) -> bool:
    """Return True when the named env var is truthy.

    Tools that want to opt in to debug capture call::
        if debug_enabled("WEB_TOOLS_DEBUG"):
            with DebugSession("web_search", args=args) as ds:
                ...
    """
    return is_truthy_value(os.environ.get(env_var))
