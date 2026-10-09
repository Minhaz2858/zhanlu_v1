"""A2A task lifecycle — store, executor registry, and event bus.

A task row is created on ``tasks/send``; a pluggable executor runs the agent
(background) and updates status/artifacts. The default executor routes the
incoming message through the Synexia FSM so external delegations get the
full governed pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from app.models.a2a_task import A2aTask

logger = logging.getLogger(__name__)

# ── Event bus (in-process) for SSE streaming ────────────────────────────
_task_event_queues: dict[str, list[asyncio.Queue]] = {}


def publish(task_id: str, event: dict[str, Any]) -> None:
    for queue in list(_task_event_queues.get(task_id, [])):
        try:
            queue.put_nowait(event)
        except Exception:  # noqa: BLE001
            pass


def subscribe(task_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _task_event_queues.setdefault(task_id, []).append(queue)
    return queue


def unsubscribe(task_id: str, queue: asyncio.Queue) -> None:
    queues = _task_event_queues.get(task_id, [])
    if queue in queues:
        queues.remove(queue)
    if not queues:
        _task_event_queues.pop(task_id, None)


class A2aTaskStore:
    """CRUD for A2aTask rows."""

    def __init__(self, db) -> None:
        self.db = db

    def create(
        self,
        *,
        agent_app_id: str,
        message_parts: list[dict[str, Any]],
        metadata: dict[str, Any] | None,
        external: bool,
        created_by_id: str | None,
        task_id: str | None = None,
    ) -> A2aTask:
        task = A2aTask(
            id=task_id or str(uuid4()),
            agent_app_id=agent_app_id,
            external=external,
            status="submitted",
            message_parts_json=json.dumps(message_parts, ensure_ascii=False, default=str),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
            created_by_id=created_by_id,
            org_id="default-org",
            app_id="default-app",
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def get(self, task_id: str) -> A2aTask | None:
        return (
            self.db.query(A2aTask)
            .filter(A2aTask.id == task_id, A2aTask.is_deleted == False)  # noqa: E712
            .first()
        )

    def update_status(self, task: A2aTask, status: str, error: str | None = None) -> None:
        task.status = status
        if error:
            task.error = error
        self.db.commit()
        self.db.refresh(task)
        publish(task.id, {"type": "status", "status": status, "error": error})

    def append_artifact(self, task: A2aTask, artifact: dict[str, Any]) -> None:
        payload = self._result(task)
        payload.setdefault("artifacts", []).append(artifact)
        task.result_json = json.dumps(payload, ensure_ascii=False, default=str)
        self.db.commit()
        publish(task.id, {"type": "artifact", "artifact": artifact})

    def append_message(self, task: A2aTask, message: dict[str, Any]) -> None:
        payload = self._result(task)
        payload.setdefault("messages", []).append(message)
        task.result_json = json.dumps(payload, ensure_ascii=False, default=str)
        self.db.commit()

    def _result(self, task: A2aTask) -> dict[str, Any]:
        if not task.result_json:
            return {}
        try:
            payload = json.loads(task.result_json)
            return payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError):
            return {}

    def to_dict(self, task: A2aTask) -> dict[str, Any]:
        parts = []
        try:
            parts = json.loads(task.message_parts_json or "[]")
        except (TypeError, ValueError):
            parts = []
        return {
            "id": task.id,
            "agent_id": task.agent_app_id,
            "status": task.status,
            "message": {"role": "user", "parts": parts},
            "artifacts": self._result(task).get("artifacts", []),
            "messages": self._result(task).get("messages", []),
            "error": task.error,
            "external": task.external,
        }

    def cancel(self, task: A2aTask) -> None:
        # Cancel always wins — external callers may abort even a completed
        # task so the record reflects the caller's intent.
        if task.status == "canceled":
            return
        task.status = "canceled"
        self.db.commit()
        self.db.refresh(task)
        publish(task.id, {"type": "status", "status": "canceled"})


# ── Executor registry ───────────────────────────────────────────────────

class A2aExecutor(Protocol):
    async def run(self, *, task: A2aTask, agent_app: Any, db) -> None: ...


def _enabled_endpoints(db) -> list:
    """Enabled LLM endpoints ordered default-first (provider ladder for
    delegated turns). Never raises; empty list → legacy default routing."""
    try:
        from app.config import settings
        from app.models.llm_model import LlmModel
        from app.services.crypto_utils import decrypt_value
        from app.services.llm_router import LLMEndpoint

        rows = (
            db.query(LlmModel)
            .filter(
                LlmModel.enabled.is_(True),
                LlmModel.is_deleted.is_(False),
            )
            .order_by(LlmModel.is_default.desc(), LlmModel.created_date.asc())
            .all()
        )
        out = []
        for row in rows:
            try:
                api_key = decrypt_value(row.api_key) or ""
            except Exception:  # noqa: BLE001
                api_key = ""
            if not api_key:
                continue  # a keyless endpoint can only produce illegal headers
            out.append(LLMEndpoint(
                base_url=row.base_url or "",
                api_key=api_key,
                model_id=row.model_id or "",
                is_private=bool(row.is_private),
                provider=row.provider or "",
                context_window=getattr(row, "context_window", None),
                max_output_tokens=getattr(row, "max_output_tokens", None),
            ))
        return out
    except Exception as e:  # noqa: BLE001 — fail-soft to legacy routing
        logger.debug("a2a: endpoint ladder unavailable: %s", e)
        return []


class DefaultA2aExecutor:
    """Runs the delegated turn through the governed ReAct sub-agent loop.

    2026-10-09 root-fix: the executor previously ran the Synexia FSM, whose
    finalize step returned empty/canned assistant content for open-ended
    questions — external partners got "I've processed your request." with a
    `completed` status. Now the delegation runs the SAME governed sub-agent
    loop used for internal `delegate_task` (self-healing tools, bounded
    iterations, guard fixes), and an empty response is an explicit task
    failure, never a canned success."""

    async def run(self, *, task: A2aTask, agent_app: Any, db) -> None:
        store = A2aTaskStore(db)
        store.update_status(task, "working")
        try:
            parts = json.loads(task.message_parts_json or "[]")
            text = " ".join(
                p.get("text", "") for p in parts
                if isinstance(p, dict) and p.get("text")
            ).strip() or "Continue."
            publish(task.id, {"type": "status", "status": "working"})

            from app.services.tool_handlers.delegate_tool import (
                _run_sub_agent_inner,
            )

            # Provider ladder: try each enabled model (default first) so a
            # dead/out-of-credit provider doesn't fail the delegation.
            last_error = None
            result = None
            endpoints = _enabled_endpoints(db) or [None]
            for endpoint in endpoints:
                result = await _run_sub_agent_inner(
                    task=text,
                    agent_name=agent_app.name,
                    db=db,
                    user_id="a2a-delegation",
                    max_iterations=6,
                    endpoint=endpoint,
                )
                if result.get("success") and (result.get("response") or "").strip():
                    break
                last_error = result.get("error")
            else:
                store.update_status(
                    task, "failed",
                    error=last_error or "agent returned an empty response",
                )
                return
            response = (result.get("response") or "").strip()
            if not response or result.get("success") is not True:
                error = result.get("error") or "agent returned an empty response"
                store.update_status(task, "failed", error=str(error))
                return
            store.update_status(task, "completed")
            store.append_artifact(task, {
                "name": "reply",
                "mimeType": "text/plain",
                "text": response,
            })
            store.append_message(task, {
                "role": "assistant",
                "parts": [{"kind": "text", "text": response}],
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("A2A task %s failed: %s", task.id, exc)
            store.update_status(task, "failed", error=str(exc))


_executors: dict[str, A2aExecutor] = {"default": DefaultA2aExecutor()}
_running: dict[str, asyncio.Task] = {}


def get_executor(name: str = "default") -> A2aExecutor:
    return _executors.get(name, _executors["default"])


def set_executor(name: str, executor: A2aExecutor) -> None:
    _executors[name] = executor


async def dispatch_task(db, task_id: str) -> None:
    """Resolve the agent row and run the executor (background entry point)."""
    from app.models.agent_app import AgentApp

    task = A2aTaskStore(db).get(task_id)
    if task is None or task.status == "canceled":
        return
    agent = (
        db.query(AgentApp)
        .filter(AgentApp.id == task.agent_app_id, AgentApp.is_deleted == False)  # noqa: E712
        .first()
    )
    if agent is None:
        A2aTaskStore(db).update_status(task, "failed", error="AgentApp not found")
        return
    executor = get_executor()
    try:
        await executor.run(task=task, agent_app=agent, db=db)
    except Exception as exc:  # noqa: BLE001
        A2aTaskStore(db).update_status(task, "failed", error=str(exc))
    finally:
        _running.pop(task.id, None)


def spawn_dispatch(db, task_id: str) -> None:
    """Fire-and-forget dispatch (wraps the async work)."""
    try:
        loop = asyncio.get_running_loop()
        _running[task_id] = loop.create_task(dispatch_task(db, task_id))
    except RuntimeError:
        # No running loop (sync context) — run inline; tests rely on this.
        asyncio.run(dispatch_task(db, task_id))


__all__ = [
    "A2aTaskStore",
    "DefaultA2aExecutor",
    "get_executor",
    "set_executor",
    "dispatch_task",
    "spawn_dispatch",
    "publish",
    "subscribe",
    "unsubscribe",
]
