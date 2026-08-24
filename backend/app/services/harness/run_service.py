"""AgentRunService — lifecycle management for agent runs.

Provides:
- ``start_run()``: create a run record and optionally execute inline
- ``get_run()`` / ``collect_run()``: query / poll for completion
- ``drain_agent_run_tasks()``: background worker for queued runs
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.database import SessionLocal

logger = logging.getLogger(__name__)

# Queue type used for agent_run tasks (matches enqueue/dequeue key)
AGENT_RUN_TASK_TYPE = "agent_run"


class AgentRunService:
    """Service managing the lifecycle of agent runs.

    All DB writes are defensive — failures are logged, never raised
    into the hot path."""

    # ------------------------------------------------------------------
    # Run creation
    # ------------------------------------------------------------------

    def create_run_record(
        self,
        *,
        agent_name: str,
        task: str,
        mode: str = "inline",
        run_id: str | None = None,
        parent_run_id: str | None = None,
        caller_context: dict | None = None,
    ) -> str:
        """Create a persisted run record. Returns run_id."""
        from app.models.agent_run import AgentRun

        run_id = run_id or uuid.uuid4().hex[:32]
        now = datetime.now(timezone.utc)

        db = SessionLocal()
        try:
            record = AgentRun(
                run_id=run_id,
                agent_name=agent_name,
                task=task,
                status="queued",
                mode=mode,
                parent_run_id=parent_run_id,
                caller_context=(
                    json.dumps(caller_context, ensure_ascii=False, default=str)
                    if caller_context
                    else None
                ),
                org_id=caller_context.get("org_id", "default-org") if caller_context else "default-org",
                app_id=caller_context.get("app_id", "default-app") if caller_context else "default-app",
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            return run_id
        except Exception as e:
            db.rollback()
            logger.warning("AgentRunService: failed to create run record: %s", e)
            return run_id  # return run_id even if persistence failed (best-effort)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Inline execution (sync-to-caller)
    # ------------------------------------------------------------------

    async def start_run(
        self,
        *,
        agent_name: str,
        task: str,
        mode: str = "inline",
        run_id: str | None = None,
        parent_run_id: str | None = None,
        caller_context: dict | None = None,
        orchestrator_kwargs: dict | None = None,
    ) -> str:
        """Start a run, returning the run_id.

        If mode=="inline", this blocks until the run completes
        (the run_store callback persists result into the DB).
        If mode=="queued", the task is enqueued to Redis/memory queue
        and control returns immediately.

        orchestrator_kwargs is passed directly to AgentRunOrchestrator
        (llm_fn, tool_dispatcher, system_prompt, tool_schemas, etc.).
        """
        run_id = run_id or uuid.uuid4().hex[:32]
        self.create_run_record(
            agent_name=agent_name,
            task=task,
            mode=mode,
            run_id=run_id,
            parent_run_id=parent_run_id,
            caller_context=caller_context,
        )

        if mode == "queued":
            self._enqueue_agent_run(agent_name, task, run_id, orchestrator_kwargs or {})
            return run_id

        # --- inline mode ---
        await self._execute_inline(
            agent_name=agent_name,
            task=task,
            run_id=run_id,
            orchestrator_kwargs=orchestrator_kwargs or {},
        )
        return run_id

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict | None:
        """Return the run record as a dict, or None."""
        from app.models.agent_run import AgentRun

        db = SessionLocal()
        try:
            record = (
                db.query(AgentRun)
                .filter(AgentRun.run_id == run_id, AgentRun.is_deleted == False)
                .first()
            )
            if record is None:
                return None
            return {
                "run_id": record.run_id,
                "agent_name": record.agent_name,
                "task": record.task,
                "status": record.status,
                "mode": record.mode,
                "result": record.result,
                "tool_calls": record.tool_calls,
                "tool_call_count": record.tool_call_count,
                "iterations": record.iterations,
                "parent_run_id": record.parent_run_id,
                "error": record.error,
                "started_at": record.started_at.isoformat() if record.started_at else None,
                "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            }
        except Exception as e:
            logger.warning("AgentRunService: get_run(%s) failed: %s", run_id, e)
            return None
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Collect (poll until complete)
    # ------------------------------------------------------------------

    async def collect_run(
        self,
        run_id: str,
        timeout: float = 120.0,
        poll_interval: float = 1.0,
    ) -> dict:
        """Poll for a run to reach terminal status.

        Returns the final run record. If timeout expires, returns the
        last known state with status possibly still "running"/"queued".
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            record = self.get_run(run_id)
            if record is None:
                await asyncio.sleep(poll_interval)
                continue
            if record["status"] in ("completed", "failed"):
                return record
            await asyncio.sleep(poll_interval)

        # Timeout — return whatever we have
        last = self.get_run(run_id)
        return last or {"run_id": run_id, "status": "unknown", "error": "collect timeout"}

    # ------------------------------------------------------------------
    # Crash recovery (P2)
    # ------------------------------------------------------------------

    def get_last_step(self, run_id: str):
        """Return the last AgentRunStep for run_id, or None."""
        from app.models.agent_run_step import AgentRunStep
        db = SessionLocal()
        try:
            return (db.query(AgentRunStep)
                    .filter(AgentRunStep.run_id == run_id, AgentRunStep.is_deleted == False)
                    .order_by(AgentRunStep.step_index.desc()).first())
        except Exception as e:
            logger.warning('AgentRunService: get_last_step(%s) failed: %s', run_id, e)
            return None
        finally:
            db.close()

    def list_steps(self, run_id: str, db=None) -> list:
        """Return all AgentRunStep records for ``run_id``, ordered by
        ``step_index`` ascending.  P0-3 router endpoint backing.
        Optional ``db`` parameter for tests; production uses SessionLocal.
        """
        from app.models.agent_run_step import AgentRunStep
        own = db is None
        if own:
            db = SessionLocal()
        try:
            return (db.query(AgentRunStep)
                    .filter(AgentRunStep.run_id == run_id,
                            AgentRunStep.is_deleted == False)
                    .order_by(AgentRunStep.step_index.asc()).all())
        except Exception as e:
            logger.warning('AgentRunService: list_steps(%s) failed: %s', run_id, e)
            return []
        finally:
            if own:
                db.close()

    def append_step(self, run_id: str, step_type: str,
                    step_index: int = 0, tool_name: str | None = None,
                    tool_input: str | None = None, tool_output: str | None = None,
                    iteration: int = 0, duration_ms: int = 0,
                    checkpoint: str | None = None, db=None) -> str | None:
        """Append a new AgentRunStep to the run.  Returns the new step_id
        on success, None on failure (logs warning).  Optional ``db`` for tests.
        """
        import uuid as _uuid
        from app.models.agent_run_step import AgentRunStep
        step_id = _uuid.uuid4().hex[:32]
        own = db is None
        if own:
            db = SessionLocal()
        try:
            step = AgentRunStep(
                step_id=step_id,
                run_id=run_id,
                step_type=step_type,
                step_index=step_index,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output,
                iteration=iteration,
                duration_ms=duration_ms,
                checkpoint=checkpoint,
            )
            db.add(step)
            db.commit()
            return step_id
        except Exception as e:
            logger.warning('AgentRunService: append_step(%s, idx=%d) failed: %s',
                           run_id, step_index, e)
            db.rollback()
            return None
        finally:
            if own:
                db.close()

    def finalize_run(self, run_id: str, status: str = 'completed',
                     iterations: int | None = None,
                     tool_call_count: int | None = None, db=None) -> bool:
        """Mark a run as terminated with ``status`` ('completed' or 'failed').
        Optional counters override the row's values.  Returns True on success.
        Optional ``db`` for tests.
        """
        from app.models.agent_run import AgentRun
        from datetime import datetime, timezone
        own = db is None
        if own:
            db = SessionLocal()
        try:
            row = db.query(AgentRun).filter(
                AgentRun.run_id == run_id, AgentRun.is_deleted == False
            ).first()
            if not row:
                return False
            row.status = status
            row.finished_at = datetime.now(timezone.utc)
            if iterations is not None:
                row.iterations = iterations
            if tool_call_count is not None:
                row.tool_call_count = tool_call_count
            db.commit()
            return True
        except Exception as e:
            logger.warning('AgentRunService: finalize_run(%s) failed: %s', run_id, e)
            db.rollback()
            return False
        finally:
            if own:
                db.close()

    def list_runs_by_conversation(self, conversation_id: str, db=None) -> list:
        """Return all AgentRun records whose ``caller_context`` JSON contains
        ``conversation_id``.  P0-3 router endpoint backing.  Optional ``db``
        for tests.
        """
        import json as _json
        from app.models.agent_run import AgentRun
        own = db is None
        if own:
            db = SessionLocal()
        try:
            rows = (db.query(AgentRun)
                    .filter(AgentRun.is_deleted == False)
                    .order_by(AgentRun.created_date.desc()).all())
            matches = []
            for r in rows:
                if not r.caller_context:
                    continue
                try:
                    ctx = _json.loads(r.caller_context) if isinstance(r.caller_context, str) else r.caller_context
                except Exception:
                    continue
                if isinstance(ctx, dict) and ctx.get('conversation_id') == conversation_id:
                    matches.append(r)
            return matches
        except Exception as e:
            logger.warning('AgentRunService: list_runs_by_conversation(%s) failed: %s',
                           conversation_id, e)
            return []
        finally:
            if own:
                db.close()

    def list_runs(self, status: str | None = None) -> list:
        """Return all AgentRun records, optionally filtered by status."""
        from app.models.agent_run import AgentRun
        db = SessionLocal()
        try:
            q = db.query(AgentRun).filter(AgentRun.is_deleted == False)
            if status:
                q = q.filter(AgentRun.status == status)
            return q.all()
        except Exception as e:
            logger.warning('AgentRunService: list_runs failed: %s', e)
            return []
        finally:
            db.close()

    def resume_run(self, run_id: str) -> dict | None:
        """Resume a crashed run from its last checkpoint snapshot.

        Returns None when the run cannot be resumed (not found, not crashed,
        or no checkpoint steps available).
        """
        runs = self.list_runs()
        target = next((r for r in runs if r.run_id == run_id), None)
        if target is None or target.status != 'crashed':
            return None
        last_step = self.get_last_step(run_id)
        if last_step is None or not last_step.messages_snapshot:
            logger.warning('AgentRunService: resume_run(%s) no checkpoint snapshot, cannot resume', run_id)
            return None
        task_msg: list[dict] = [{'role': 'user', 'content': target.task or ''}]
        try:
            parsed = json.loads(last_step.messages_snapshot)
            if isinstance(parsed, list) and len(parsed) > 0:
                task_msg = parsed
        except (json.JSONDecodeError, TypeError):
            logger.warning('AgentRunService: resume_run(%s) corrupted snapshot, cannot resume', run_id)
            return None
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
        return loop.run_until_complete(
            self._run_inline(agent_name=target.agent_name or 'unknown',
                             task=target.task or '', run_id=run_id, messages=task_msg))

    async def _run_inline(self, *, agent_name, task, run_id, messages=None, **kwargs):
        """Re-usable inline runner for resume (mockable in tests)."""
        return await self.start_run(agent_name=agent_name, task=task,
                                    mode='inline', run_id=run_id,
                                    orchestrator_kwargs=kwargs)

    # ------------------------------------------------------------------
    # Background drain worker
    # ------------------------------------------------------------------

    @staticmethod
    async def drain_agent_run_tasks(*, orch_factory=None, poll_interval: float = 2.0):
        """Loop forever, draining agent_run tasks from the queue.

        Parameters
        ----------
        orch_factory : Callable[..., Awaitable] | None
            Async callable ``(agent_name, task, run_id, **kw) -> RunResult``.
            If None, logs a warning and skips.
        poll_interval : float
            Seconds to sleep when the queue is empty.
        """
        from app.services import task_queue

        logger.info("AgentRunService: drain worker started (poll=%ss)", poll_interval)
        while True:
            try:
                task_info = task_queue.dequeue(AGENT_RUN_TASK_TYPE, timeout=int(poll_interval))
                if task_info is None:
                    continue

                payload = task_info.payload or {}
                run_id = payload.get("run_id", task_info.task_id)
                agent_name = str(payload.get("agent_name", "unknown"))
                task_str = str(payload.get("task", ""))

                try:
                    logger.info(
                        "AgentRunService: executing queued run %s (%s)",
                        run_id, agent_name,
                    )
                    if orch_factory:
                        await orch_factory(
                            agent_name=agent_name,
                            task=task_str,
                            run_id=run_id,
                        )
                    else:
                        logger.warning(
                            "AgentRunService: no orch_factory for run %s — skipping",
                            run_id,
                        )
                    task_queue.mark_complete(task_info.task_id)
                except Exception as e:
                    logger.exception(
                        "AgentRunService: queued run %s failed: %s",
                        run_id, e,
                    )
                    task_queue.mark_failed(task_info.task_id, error=str(e))

            except asyncio.CancelledError:
                logger.info("AgentRunService: drain worker cancelled")
                break
            except Exception:
                logger.exception("AgentRunService: drain worker error — retrying")
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _execute_inline(
        self,
        *,
        agent_name: str,
        task: str,
        run_id: str,
        orchestrator_kwargs: dict,
    ):
        """Create orchestrator, run, persist result via run_store."""
        orig_run_store = orchestrator_kwargs.pop("run_store", None)

        def _persist_run_store(event: str, payload: dict):
            if event == "start":
                self._update_run_started(run_id)
            elif event == "finish":
                self._update_run_finished(run_id, payload)
            if orig_run_store:
                try:
                    orig_run_store(event, payload)
                except Exception:
                    pass

        from app.services.harness.orchestrator import AgentRunOrchestrator

        orch = AgentRunOrchestrator(
            agent_name=agent_name,
            task=task,
            run_store=_persist_run_store,
            run_id=run_id,
            **orchestrator_kwargs,
        )
        await orch.run()

    def _update_run_started(self, run_id: str):
        from app.models.agent_run import AgentRun

        db = SessionLocal()
        try:
            record = (
                db.query(AgentRun)
                .filter(AgentRun.run_id == run_id, AgentRun.is_deleted == False)
                .first()
            )
            if record:
                record.status = "running"
                record.started_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("AgentRunService: update_started(%s) failed: %s", run_id, e)
        finally:
            db.close()

    def _update_run_finished(self, run_id: str, payload: dict):
        from app.models.agent_run import AgentRun

        db = SessionLocal()
        try:
            record = (
                db.query(AgentRun)
                .filter(AgentRun.run_id == run_id, AgentRun.is_deleted == False)
                .first()
            )
            if record:
                record.status = "completed" if payload.get("success") else "failed"
                record.result = payload.get("answer", "")
                record.tool_calls = json.dumps(
                    payload.get("tool_calls", []), ensure_ascii=False, default=str
                )
                record.tool_call_count = payload.get("tool_call_count", 0)
                record.iterations = payload.get("iterations", 0)
                record.error = payload.get("error")
                record.finished_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("AgentRunService: update_finished(%s) failed: %s", run_id, e)
        finally:
            db.close()

    def _enqueue_agent_run(
        self, agent_name: str, task: str, run_id: str, orch_kwargs: dict
    ):
        from app.services import task_queue

        task_queue.enqueue(
            AGENT_RUN_TASK_TYPE,
            {
                "run_id": run_id,
                "agent_name": agent_name,
                "task": task,
                "orch_kwargs": json.dumps(orch_kwargs, ensure_ascii=False, default=str),
            },
        )


# ---- Singleton ----

_svc: AgentRunService | None = None


def get_run_service() -> AgentRunService:
    global _svc
    if _svc is None:
        _svc = AgentRunService()
    return _svc
