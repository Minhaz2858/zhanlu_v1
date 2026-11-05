"""R6 Phase 4 — subprocess runner child entrypoint (§6.5, F-22).

Run as ``python -m app.services.automation.subprocess_child`` (spawned by
``subprocess_runner.launch_subprocess_runner``). The job spec arrives via
the ``AUTOMATION_CHILD_JOB`` env var (stdin is RESERVED for parent ACKs).

Ownership (normative, §6.5/§11.4): the child runs the agent harness through
the §5 invocation contract and emits typed frames on stdout; it NEVER
touches automation tables (executions, occurrences, events, leases,
delivery). All persistence — state, fencing, events, delivery — belongs to
the parent (``ParentEventBroker``). Critical lifecycle events block until
the parent's post-commit ACK (exactly-durable); telemetry is bounded-loss.
"""
from __future__ import annotations

import json
import logging
import os
import sys

from app.services.automation.subprocess_runner import ChildEventEmitter

logger = logging.getLogger(__name__)


def main() -> int:
    raw = os.environ.get("AUTOMATION_CHILD_JOB")
    if not raw:
        print(json.dumps({"child_error": "missing AUTOMATION_CHILD_JOB"}),
              file=sys.stderr)
        return 2
    job = json.loads(raw)

    child = ChildEventEmitter(
        execution_id=job["execution_id"],
        attempt=int(job.get("attempt", 0)),
        event_stream=sys.stdout.buffer,
        ack_stream=sys.stdin.buffer,
    )

    async def _run() -> None:
        from app.services.agent_invocation import (
            AgentInvocationContext, AgentInvocationRequest, AutomationRef,
            CancellationSignal, ExecutionBudgets, invoke_agent,
        )
        from app.database import SessionLocal
        from app.models.user import User

        db = SessionLocal()
        try:
            user = (db.query(User)
                    .filter(User.id == job["user_id"]).one_or_none())
            if user is None:
                # Fail honestly BEFORE the heavy harness import — an invalid
                # job is a protocol error, never a fabricated success.
                child.emit("run_failed",
                           {"classification": "error",
                            "error": f"user {job['user_id']!r} not found"},
                           critical=True)
                raise SystemExit(3)
            ctx = AgentInvocationContext(
                db=db, user=user,
                org_id=job["org_id"], app_id=job["app_id"],
                project_id=job.get("project_id"),
                conversation_id=job["conversation_id"],
                automation=AutomationRef(
                    definition_id=job["automation_task_id"],
                    execution_id=job["execution_id"],
                    attempt=int(job.get("attempt", 0)),
                    spec_version=job.get("spec_version", "v1"),
                    trigger=job.get("trigger", "schedule"),
                ),
                cancellation=CancellationSignal(
                    deadline=job.get("deadline")),
                budgets=ExecutionBudgets(**(job.get("budgets") or {})),
                trace_id=job.get("trace_id") or "",
            )
            req = AgentInvocationRequest(
                message=job["message"],
                force_planning=bool(job.get("force_planning", True)),
            )
            content_parts: list[str] = []
            async for evt in invoke_agent(ctx, req):
                if evt.type == "delta":
                    # §6.2b ordering: the child accumulates the answer so the
                    # terminal frame carries the FULL output — the parent
                    # persists it BEFORE the fenced finalizing CAS.
                    content_parts.append(evt.payload.get("content") or "")
                    child.emit("delta", evt.payload)
                elif evt.type in ("run_finished", "run_failed"):
                    payload = dict(evt.payload)
                    if content_parts:
                        payload["content"] = "".join(content_parts)
                    child.emit(evt.type, payload, critical=True)
                else:
                    # telemetry — bounded-loss, never blocks the run
                    child.emit(evt.type, evt.payload)
        finally:
            db.close()

    import asyncio
    try:
        asyncio.run(_run())
        return 0
    except Exception as e:  # noqa: BLE001 — the child reports, never fakes
        logger.exception("subprocess child failed")
        try:
            child.emit("run_failed",
                       {"classification": "error", "error": str(e)},
                       critical=True)
        except Exception:  # noqa: BLE001 — parent may already be gone
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
