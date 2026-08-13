"""SkillExecutionRecorder — writes SkillRun DB records for every skill invocation.

This is the **automatic instrumentation** layer: every time a skill is
loaded or executed through a runtime tool handler (``load_skill_body``,
``skills`` load/execute/run), a ``SkillRun`` row is inserted capturing
the skill name, agent, conversation, status, timing, and error.

Design principles:
  - **Non-blocking**: all failures are caught and logged at debug level.
    A recording failure must NEVER break a skill load/execute.
  - **Independent session**: uses its own ``SessionLocal()`` so it doesn't
    interfere with the handler's transaction state.
  - **Context-aware**: pulls ``conversation_id`` and ``agent_name`` from
    the tool handler's ``context`` dict.

The ``SkillRun`` model (``app.models.skill_run``) already exists with a
migration — no schema changes needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class SkillExecutionRecorder:
    """Records SkillRun DB entries for every skill load/execute.

    All methods are static and never raise — instrumentation is best-effort.
    """

    @staticmethod
    def record(
        skill_name: str,
        action: str,
        status: str,
        conversation_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        execution_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        input_json: Optional[dict] = None,
        output_json: Optional[dict] = None,
    ) -> None:
        """Insert a SkillRun row. Never raises.

        Args:
            skill_name: The name of the skill that was loaded/executed.
            action: What happened — "load", "execute", "run", or "create".
            status: "completed" or "failed".
            conversation_id: The conversation where the invocation occurred.
            agent_name: The agent that invoked the skill.
            execution_id: The automation execution id (when run inside a task).
            duration_ms: How long the operation took.
            error_message: Error details if status == "failed".
            input_json: The input arguments (optional, for debugging).
            output_json: A summary of the output (optional).
        """
        try:
            from app.database import SessionLocal
            from app.models.skill_run import SkillRun
            from uuid import uuid4

            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                run = SkillRun(
                    id=str(uuid4()),
                    skill_profile_id=None,  # We track by name; profile link is optional
                    conversation_id=conversation_id,
                    execution_id=execution_id,
                    status=status,
                    input_json={"action": action, **(input_json or {})} if input_json else {"action": action},
                    output_json=output_json,
                    result_text=None,
                    error_message=error_message,
                    started_at=now,
                    completed_at=now,
                    duration_ms=duration_ms,
                    attempt_number=1,
                )
                # Store skill_name and agent_name in input_json since the
                # SkillRun model doesn't have dedicated columns for them
                # (skill_profile_id is the FK, but we track by name for
                # skills that don't have a SkillProfile yet).
                if run.input_json is None:
                    run.input_json = {}
                run.input_json["skill_name"] = skill_name
                run.input_json["agent_name"] = agent_name or "unknown"
                run.input_json["action"] = action

                db.add(run)
                db.commit()
                logger.debug(
                    "SkillExecutionRecorder: recorded %s/%s status=%s conv=%s",
                    skill_name, action, status, conversation_id,
                )
            finally:
                db.close()
        except Exception as exc:
            logger.debug("SkillExecutionRecorder: failed to record (non-fatal): %s", exc)

    @staticmethod
    def record_from_context(
        skill_name: str,
        action: str,
        status: str,
        context: Optional[dict] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        input_json: Optional[dict] = None,
        output_json: Optional[dict] = None,
    ) -> None:
        """Convenience wrapper: extract conversation_id/agent_name/execution_id
        from context (with a contextvar fallback for automation runs)."""
        ctx = context or {}

        # execution_id may be present in the tool context directly, or we can
        # read it from the automation executor's contextvar (set while a task
        # is running). Lazy import avoids a circular dependency at load time.
        execution_id = ctx.get("execution_id")
        if not execution_id:
            try:
                from app.services.automation_executor import get_current_execution_id
                execution_id = get_current_execution_id()
            except Exception:
                execution_id = None

        SkillExecutionRecorder.record(
            skill_name=skill_name,
            action=action,
            status=status,
            conversation_id=ctx.get("conversation_id"),
            agent_name=ctx.get("agent_name"),
            execution_id=execution_id,
            duration_ms=duration_ms,
            error_message=error_message,
            input_json=input_json,
            output_json=output_json,
        )
