"""CheckpointSink — persist AgentRunStep rows for each execution event.

Fire-and-forget design: every DB write is a best-effort side effect.
Failures are logged at WARNING and never raised into the agent loop.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class CheckpointSink:
    """Persist one ``AgentRunStep`` per LLM-call / tool-call event.

    Drop-in ``EventSink`` compatible callable — receives event dicts
    emitted by ``AgentRunOrchestrator`` and writes rows to the
    ``agent_run_steps`` table.  Activate by passing as ``event_sink``
    when constructing the orchestrator.
    """

    _TRUNCATE_MESSAGES = 4000       # characters (≈ 4 KB)
    _TRUNCATE_PREVIEW = 4000

    def __init__(self, *, enabled: bool = True):
        self._enabled = enabled
        self._step_index: int = 0

    def __call__(self, event: dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            self._handle(event)
        except Exception as e:
            logger.warning("CheckpointSink: failed to persist step (%s): %s",
                           event.get("type", "?"), e)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _handle(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype in ("llm_call", "tool_call", "synthesis"):
            self._step_index += 1
            self._persist_step(etype, event)

    def _persist_step(self, step_type: str, event: dict[str, Any]) -> None:
        from app.models.agent_run_step import AgentRunStep
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            step = AgentRunStep(
                step_id=uuid.uuid4().hex[:32],
                run_id=event.get("run_id", ""),
                step_type=step_type,
                step_index=self._step_index,
                iteration=event.get("iteration", 0),
                duration_ms=event.get("duration_ms"),
                tool_name=event.get("tool_name"),
                tool_args=self._safe_json(event.get("tool_args")),
                result_preview=self._truncate(
                    event.get("result_preview", ""),
                    self._TRUNCATE_PREVIEW,
                ),
                messages_snapshot=self._truncate(
                    self._safe_json(event.get("messages")),
                    self._TRUNCATE_MESSAGES,
                ),
                prompt_tokens=event.get("prompt_tokens"),
                completion_tokens=event.get("completion_tokens"),
                total_tokens=event.get("total_tokens"),
                status=event.get("status", "ok"),
                error=self._truncate(event.get("error"), self._TRUNCATE_PREVIEW),
                retry_count=event.get("retry_count", 0),
            )
            db.add(step)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_json(obj: Any) -> str | None:
        if obj is None:
            return None
        try:
            return json.dumps(obj, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(obj)[:CheckpointSink._TRUNCATE_PREVIEW]

    @staticmethod
    def _truncate(text: str | None, max_len: int) -> str | None:
        if text is None:
            return None
        if len(text) <= max_len:
            return text
        return text[:max_len]
