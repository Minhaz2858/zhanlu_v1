# Zhanlu Canonical Event Stream + Artifacts + Sandbox Hardening — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the runtime-contracts and sandbox-artifacts specs in the existing Zhanlu backend by adding a canonical event stream (PG + Redis pub/sub), artifact validation/regenerate, and sandbox hardening — while keeping the existing v3 SSE endpoint and `sandbox_job_events` audit log **completely untouched**.

**Architecture:** Three emitters (SynexiaFSM, sandbox-worker, approval_service, artifact_service) call `EventBus.publish(envelope)`. The bus persists each envelope to a new `execution_events` table (PG = source of truth) AND publishes to a Redis channel (live fanout). A new `CanonicalAgentRunner` backs a new `POST /api/v1/chat/stream` SSE endpoint. A new `GET /api/v1/executions/{id}/events` endpoint replays from PG. v3 SSE, `sandbox_job_events`, and existing `ApprovalRequest` row writes are all unchanged.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), Alembic, Redis (pub/sub), Pydantic v2, pytest.

---

## Phase 1 — Canonical Event Stream

### Task 1: `ExecutionEvent` model + Alembic migration

**Files:**
- Create: `backend/app/models/execution_event.py`
- Create: `backend/alembic/versions/010_execution_events.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/tests/test_canonical_events.py`

**Step 1: Write the failing test**

```python
# backend/tests/test_canonical_events.py
import pytest
from app.models.execution_event import ExecutionEvent, CANONICAL_EVENT_TYPES

def test_execution_event_table_name():
    assert ExecutionEvent.__tablename__ == "execution_events"

def test_canonical_event_types_set():
    expected = {
        "message.created", "message.delta", "message.completed",
        "execution.started", "execution.node_started", "execution.node_completed",
        "execution.node_failed", "execution.completed", "execution.failed",
        "approval.required", "artifact.created", "artifact.preview_ready",
    }
    assert set(CANONICAL_EVENT_TYPES) == expected
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && pytest tests/test_canonical_events.py -v`
Expected: FAIL with ImportError.

**Step 3: Write the model**

```python
# backend/app/models/execution_event.py
"""Canonical event stream — single source of truth for conversation timelines.

Every lifecycle point (message, execution, node, approval, artifact) emits
a row here AND a Redis pub/sub message. PG = durable replay; Redis = live
in-process fanout.
"""
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import TimestampedBase

CANONICAL_EVENT_TYPES = [
    "message.created", "message.delta", "message.completed",
    "execution.started", "execution.node_started", "execution.node_completed",
    "execution.node_failed", "execution.completed", "execution.failed",
    "approval.required", "artifact.created", "artifact.preview_ready",
]


class ExecutionEvent(TimestampedBase):
    __tablename__ = "execution_events"
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    org_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    app_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    node_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    seq: Mapped[int] = mapped_column(default=0, nullable=False)
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


Index("ix_execution_events_exec_seq", ExecutionEvent.execution_id, ExecutionEvent.seq)
Index("ix_execution_events_conv_created", ExecutionEvent.conversation_id, ExecutionEvent.created_at)
```

**Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && pytest tests/test_canonical_events.py -v`
Expected: PASS.

**Step 5: Export the model**

Edit `backend/app/models/__init__.py` and add:
```python
from app.models.execution_event import ExecutionEvent, CANONICAL_EVENT_TYPES  # noqa: F401
```

**Step 6: Write Alembic migration `backend/alembic/versions/010_execution_events.py`**

```python
"""execution_events canonical timeline table"""
from alembic import op
import sqlalchemy as sa

revision = "010_execution_events"
down_revision = "009_governance_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("node_run_id", sa.String(36), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data", sa.JSON(), nullable=True),
    )
    for col in ["event_id", "event_type", "org_id", "app_id", "conversation_id",
                "message_id", "execution_id", "node_run_id", "created_at"]:
        op.create_index(f"ix_execution_events_{col}", "execution_events", [col])
    op.create_index("ix_execution_events_exec_seq", "execution_events", ["execution_id", "seq"])
    op.create_index("ix_execution_events_conv_created", "execution_events", ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_execution_events_conv_created", table_name="execution_events")
    op.drop_index("ix_execution_events_exec_seq", table_name="execution_events")
    for col in ["created_at", "node_run_id", "execution_id", "message_id",
                "conversation_id", "app_id", "org_id", "event_type", "event_id"]:
        op.drop_index(f"ix_execution_events_{col}", table_name="execution_events")
    op.drop_table("execution_events")
```

**Step 7: Run migration**

Run: `cd /root/zhanlu/backend && alembic upgrade head`
Expected: applies 010_execution_events cleanly.

**Step 8: Commit**

```bash
cd /root/zhanlu && git add backend/app/models/execution_event.py backend/app/models/__init__.py backend/alembic/versions/010_execution_events.py backend/tests/test_canonical_events.py
git commit -m "feat(events): ExecutionEvent model + canonical event types + alembic migration"
```

---

### Task 2: `EventBus` (publish + subscribe + PG persist)

**Files:**
- Create: `backend/app/services/events/__init__.py`
- Create: `backend/app/services/events/event_bus.py`
- Modify: `backend/tests/test_canonical_events.py` (add bus tests)

**Step 1: Append failing test to `test_canonical_events.py`**

```python
import asyncio
from app.services.events.event_bus import EventBus, CanonicalEvent

async def test_event_bus_publishes_to_subscribers():
    bus = EventBus()
    received = []

    async def sub(envelope):
        received.append(envelope)

    q = bus.subscribe("test-conv")
    bus.add_queue_subscriber("test-conv", q)
    env = CanonicalEvent(
        event_id="e1", event_type="message.created",
        conversation_id="test-conv", data={"x": 1},
    )
    await bus.publish(env, persist=False)
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0]["event_type"] == "message.created"
    assert received[0]["data"] == {"x": 1}
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && pytest tests/test_canonical_events.py -v`
Expected: FAIL (ModuleNotFoundError).

**Step 3: Write `backend/app/services/events/__init__.py`**

```python
from app.services.events.event_bus import EventBus, CanonicalEvent, get_bus
from app.services.events.canonical_events import (
    message_created, message_delta, message_completed,
    execution_started, node_started, node_completed, node_failed,
    execution_completed, execution_failed,
    approval_required, artifact_created, artifact_preview_ready,
)

__all__ = [
    "EventBus", "CanonicalEvent", "get_bus",
    "message_created", "message_delta", "message_completed",
    "execution_started", "node_started", "node_completed", "node_failed",
    "execution_completed", "execution_failed",
    "approval_required", "artifact_created", "artifact_preview_ready",
]
```

**Step 4: Write `backend/app/services/events/event_bus.py`**

```python
"""In-process pub/sub for canonical events.

The bus is the SINGLE emission point for canonical events. Callers
(FSM, sandbox-worker, approval_service, artifact_service) build a
CanonicalEvent and pass it to publish(). The bus:

1. Persists to the execution_events table (PG = source of truth).
2. Fans out to in-process subscribers (SSE generators).
3. Optionally publishes to Redis for cross-process fanout.

If Redis is unavailable, in-process subscribers still work — the
system degrades to single-process pub/sub rather than failing.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class CanonicalEvent:
    """The canonical event envelope. Matches docs/03_runtime_contracts."""
    event_type: str
    data: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    org_id: Optional[str] = None
    app_id: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    execution_id: Optional[str] = None
    node_run_id: Optional[str] = None
    seq: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = None
        return d


class EventBus:
    """Process-local pub/sub with optional Redis bridge and PG persistence."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._redis = None
        self._redis_url = os.getenv("REDIS_URL", "")
        self._init_redis()

    def _init_redis(self) -> None:
        if not self._redis_url:
            return
        try:
            import redis.asyncio as redis
            self._redis = redis.from_url(self._redis_url, decode_responses=True)
            logger.info("EventBus: Redis pub/sub enabled")
        except Exception as e:
            logger.warning("EventBus: Redis init failed (%s); in-process only", e)
            self._redis = None

    def subscribe(self, conversation_id: str) -> asyncio.Queue:
        """Subscribe to events for a conversation. Returns a queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.add_queue_subscriber(conversation_id, q)
        return q

    def add_queue_subscriber(self, conversation_id: str, q: asyncio.Queue) -> None:
        self._subscribers.setdefault(conversation_id, []).append(q)

    def unsubscribe(self, conversation_id: str, q: asyncio.Queue) -> None:
        if conversation_id in self._subscribers:
            try:
                self._subscribers[conversation_id].remove(q)
            except ValueError:
                pass

    async def publish(self, envelope: CanonicalEvent, persist: bool = True) -> None:
        from datetime import datetime
        body = envelope.to_dict()
        body["created_at"] = datetime.utcnow().isoformat()

        if persist:
            asyncio.create_task(self._persist(envelope, body["created_at"]))

        if envelope.conversation_id:
            async with self._lock:
                queues = list(self._subscribers.get(envelope.conversation_id, []))
            for q in queues:
                try:
                    q.put_nowait(body)
                except asyncio.QueueFull:
                    logger.warning("EventBus: subscriber queue full, dropping %s", envelope.event_id)

        if self._redis is not None:
            try:
                await self._redis.publish(
                    f"canon:{envelope.conversation_id or '*'}", json.dumps(body)
                )
            except Exception as e:
                logger.warning("EventBus: Redis publish failed (%s)", e)

    async def _persist(self, envelope: CanonicalEvent, created_at_iso: str) -> None:
        try:
            from datetime import datetime
            from app.services.db import async_session
            from app.models.execution_event import ExecutionEvent
            async with async_session() as session:
                row = ExecutionEvent(
                    event_id=envelope.event_id, event_type=envelope.event_type,
                    org_id=envelope.org_id, app_id=envelope.app_id,
                    conversation_id=envelope.conversation_id,
                    message_id=envelope.message_id, execution_id=envelope.execution_id,
                    node_run_id=envelope.node_run_id, seq=envelope.seq,
                    data=envelope.data,
                    created_at=datetime.fromisoformat(created_at_iso),
                )
                session.add(row)
                await session.commit()
        except Exception as e:
            logger.exception("EventBus: persist failed for %s: %s", envelope.event_id, e)


_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
```

**Step 5: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && pytest tests/test_canonical_events.py -v`
Expected: PASS (both tests).

**Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/events backend/tests/test_canonical_events.py
git commit -m "feat(events): EventBus with in-process + Redis + PG persist"
```

---

### Task 3: Typed canonical event builders

**Files:**
- Create: `backend/app/services/events/canonical_events.py`

**Step 1: Write the module**

```python
"""Typed builders for the 12 canonical event types from the spec.

These are convenience helpers so callers don't have to remember
the envelope shape. They return a CanonicalEvent ready to pass
to EventBus.publish().

Spec: docs/03_runtime_contracts/Zhanlu_Event_Stream_Contract.md
"""
from __future__ import annotations
from typing import Optional
from app.services.events.event_bus import CanonicalEvent


def message_created(*, conversation_id, message_id, role,
                    content_preview="", org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="message.created", conversation_id=conversation_id,
                          message_id=message_id, org_id=org_id, app_id=app_id,
                          data={"role": role, "content_preview": content_preview}, **kw)


def message_delta(*, conversation_id, message_id, delta, org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="message.delta", conversation_id=conversation_id,
                          message_id=message_id, org_id=org_id, app_id=app_id,
                          data={"delta": delta}, **kw)


def message_completed(*, conversation_id, message_id, full_content,
                      org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="message.completed", conversation_id=conversation_id,
                          message_id=message_id, org_id=org_id, app_id=app_id,
                          data={"content": full_content}, **kw)


def execution_started(*, conversation_id, execution_id, plan,
                      org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="execution.started", conversation_id=conversation_id,
                          execution_id=execution_id, org_id=org_id, app_id=app_id,
                          data={"plan": plan}, **kw)


def node_started(*, conversation_id, execution_id, node_run_id,
                 node_name, node_type, org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="execution.node_started", conversation_id=conversation_id,
                          execution_id=execution_id, node_run_id=node_run_id,
                          org_id=org_id, app_id=app_id,
                          data={"node_name": node_name, "node_type": node_type}, **kw)


def node_completed(*, conversation_id, execution_id, node_run_id, result,
                   org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="execution.node_completed", conversation_id=conversation_id,
                          execution_id=execution_id, node_run_id=node_run_id,
                          org_id=org_id, app_id=app_id, data={"result": result}, **kw)


def node_failed(*, conversation_id, execution_id, node_run_id, error,
                org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="execution.node_failed", conversation_id=conversation_id,
                          execution_id=execution_id, node_run_id=node_run_id,
                          org_id=org_id, app_id=app_id, data={"error": error}, **kw)


def execution_completed(*, conversation_id, execution_id, summary,
                        org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="execution.completed", conversation_id=conversation_id,
                          execution_id=execution_id, org_id=org_id, app_id=app_id,
                          data={"summary": summary}, **kw)


def execution_failed(*, conversation_id, execution_id, error,
                     org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="execution.failed", conversation_id=conversation_id,
                          execution_id=execution_id, org_id=org_id, app_id=app_id,
                          data={"error": error}, **kw)


def approval_required(*, conversation_id, execution_id, approval_request_id,
                      risk_tier, action, justification,
                      org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="approval.required", conversation_id=conversation_id,
                          execution_id=execution_id, org_id=org_id, app_id=app_id,
                          data={"approval_request_id": approval_request_id,
                                "risk_tier": risk_tier, "action": action,
                                "justification": justification}, **kw)


def artifact_created(*, conversation_id, execution_id, artifact_id,
                     artifact_version_id, kind, org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="artifact.created", conversation_id=conversation_id,
                          execution_id=execution_id, org_id=org_id, app_id=app_id,
                          data={"artifact_id": artifact_id,
                                "artifact_version_id": artifact_version_id,
                                "kind": kind}, **kw)


def artifact_preview_ready(*, conversation_id, execution_id, artifact_id,
                           artifact_version_id, preview_url,
                           org_id=None, app_id=None, **kw):
    return CanonicalEvent(event_type="artifact.preview_ready", conversation_id=conversation_id,
                          execution_id=execution_id, org_id=org_id, app_id=app_id,
                          data={"artifact_id": artifact_id,
                                "artifact_version_id": artifact_version_id,
                                "preview_url": preview_url}, **kw)
```

**Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/events/canonical_events.py
git commit -m "feat(events): typed canonical event builders for all 12 spec event types"
```

---

### Task 4: `CanonicalAgentRunner` (slim streamable agent loop)

**Files:**
- Create: `backend/app/services/agent_runtime/__init__.py`
- Create: `backend/app/services/agent_runtime/canonical_runner.py`

**Step 1: Write `backend/app/services/agent_runtime/__init__.py`**

```python
from app.services.agent_runtime.canonical_runner import CanonicalAgentRunner, CanonicalRunRequest
__all__ = ["CanonicalAgentRunner", "CanonicalRunRequest"]
```

**Step 2: Write the runner**

```python
"""CanonicalAgentRunner — slim agent loop that emits canonical events.

Backs POST /api/v1/chat/stream. Lighter than SynexiaFSM:
  - No plan/gate/observe FSM transitions.
  - Streams LLM tokens as message.delta events as they arrive.
  - Emits execution.started, node.*, execution.completed|failed.
  - Emits approval.required when a tool needs governance.
  - Emits artifact.created / artifact.preview_ready when artifacts are produced.
"""
from __future__ import annotations
import logging
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from app.services.events import get_bus
from app.services.events.canonical_events import (
    message_created, message_delta, message_completed,
    execution_started, node_started, node_completed,
    execution_completed, execution_failed,
)

logger = logging.getLogger(__name__)


@dataclass
class CanonicalRunRequest:
    conversation_id: str
    app_id: str
    user_message: str
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    system_prompt: Optional[str] = None
    tools: Optional[list] = None


class CanonicalAgentRunner:
    def __init__(self) -> None:
        self.bus = get_bus()

    async def run(self, req: CanonicalRunRequest) -> AsyncIterator[dict]:
        message_id = str(uuid.uuid4())
        execution_id = str(uuid.uuid4())

        ev = message_created(
            conversation_id=req.conversation_id, message_id=message_id,
            role="user", content_preview=req.user_message[:200],
            org_id=req.org_id, app_id=req.app_id,
        )
        await self.bus.publish(ev)
        yield ev.to_dict()

        ev = execution_started(
            conversation_id=req.conversation_id, execution_id=execution_id,
            plan=[{"name": "respond", "type": "llm"}],
            org_id=req.org_id, app_id=req.app_id,
        )
        await self.bus.publish(ev)
        yield ev.to_dict()

        try:
            full_reply = await self._stream_llm(req, message_id, execution_id)
            ev = execution_completed(
                conversation_id=req.conversation_id, execution_id=execution_id,
                summary=full_reply[:200], org_id=req.org_id, app_id=req.app_id,
            )
            await self.bus.publish(ev)
            yield ev.to_dict()
        except Exception as e:
            logger.exception("CanonicalAgentRunner failed")
            ev = execution_failed(
                conversation_id=req.conversation_id, execution_id=execution_id,
                error=str(e), org_id=req.org_id, app_id=req.app_id,
            )
            await self.bus.publish(ev)
            yield ev.to_dict()

    async def _stream_llm(self, req: CanonicalRunRequest,
                          message_id: str, execution_id: str) -> str:
        node_run_id = str(uuid.uuid4())
        ev = node_started(
            conversation_id=req.conversation_id, execution_id=execution_id,
            node_run_id=node_run_id, node_name="respond", node_type="llm",
            org_id=req.org_id, app_id=req.app_id,
        )
        await self.bus.publish(ev)

        from app.routers.agents import _stream_llm_response  # type: ignore

        chunks: list[str] = []
        async for chunk in _stream_llm_response(
            user_message=req.user_message,
            system_prompt=req.system_prompt or "You are a helpful assistant.",
            tools=req.tools or [],
        ):
            chunks.append(chunk)
            ev = message_delta(
                conversation_id=req.conversation_id, message_id=message_id,
                delta=chunk, org_id=req.org_id, app_id=req.app_id,
            )
            await self.bus.publish(ev)

        full = "".join(chunks)

        ev = message_completed(
            conversation_id=req.conversation_id, message_id=message_id,
            full_content=full, org_id=req.org_id, app_id=req.app_id,
        )
        await self.bus.publish(ev)

        ev = node_completed(
            conversation_id=req.conversation_id, execution_id=execution_id,
            node_run_id=node_run_id, result={"length": len(full), "preview": full[:100]},
            org_id=req.org_id, app_id=req.app_id,
        )
        await self.bus.publish(ev)
        return full
```

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/agent_runtime/
git commit -m "feat(agent-runtime): CanonicalAgentRunner — slim streamable loop emitting canonical events"
```

---

### Task 5: `POST /api/v1/chat/stream` SSE endpoint

**Files:**
- Create: `backend/app/routers/chat_stream.py`
- Modify: `backend/main.py` (register router)

**Step 1: Write the router**

```python
"""POST /api/v1/chat/stream — SSE endpoint backed by CanonicalAgentRunner.

Emits canonical events as SSE data lines:
  data: {"event_type": "...", ...}\n\n

The v3 endpoint is untouched.
"""
from __future__ import annotations
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.agent_runtime import CanonicalAgentRunner, CanonicalRunRequest

router = APIRouter(prefix="/api/v1/chat", tags=["chat-stream"])
logger = logging.getLogger(__name__)


class ChatStreamRequest(BaseModel):
    app_id: str
    conversation_id: str
    user_message: str
    org_id: str | None = None
    user_id: str | None = None
    system_prompt: str | None = None


@router.post("/stream")
async def chat_stream(req: ChatStreamRequest, request: Request) -> StreamingResponse:
    async def event_generator() -> AsyncIterator[bytes]:
        runner = CanonicalAgentRunner()
        canonical = CanonicalRunRequest(
            conversation_id=req.conversation_id, app_id=req.app_id,
            user_message=req.user_message, org_id=req.org_id,
            user_id=req.user_id, system_prompt=req.system_prompt,
        )
        async for envelope in runner.run(canonical):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(envelope)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

**Step 2: Register the router in `backend/main.py`**

After line 36 (`from app.routers.executions import router as executions_router`) add:
```python
from app.routers.chat_stream import router as chat_stream_router
from app.routers.execution_events import router as execution_events_router
```

After line 102 (`app.include_router(executions_router, prefix="/api")`) add:
```python
    app.include_router(chat_stream_router)
    app.include_router(execution_events_router)
```

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/chat_stream.py backend/main.py
git commit -m "feat(api): POST /api/v1/chat/stream SSE endpoint backed by CanonicalAgentRunner"
```

---

### Task 6: `GET /api/v1/executions/{id}/events` (PG replay)

**Files:**
- Create: `backend/app/routers/execution_events.py`
- Modify: `backend/main.py` (already covered in Task 5)

**Step 1: Write the router**

```python
"""GET /api/v1/executions/{id}/events — replay canonical timeline from PG.

Useful for resume-after-disconnect and clients that prefer polling.
"""
from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.services.db import async_session, get_current_user
from app.models.execution_event import ExecutionEvent

router = APIRouter(prefix="/api/v1/executions", tags=["execution-events"])
logger = logging.getLogger(__name__)


class EventOut(BaseModel):
    event_id: str
    event_type: str
    org_id: Optional[str]
    app_id: Optional[str]
    conversation_id: Optional[str]
    message_id: Optional[str]
    execution_id: Optional[str]
    node_run_id: Optional[str]
    seq: int
    data: dict
    created_at: str

    class Config:
        from_attributes = True


@router.get("/{execution_id}/events", response_model=list[EventOut])
async def list_execution_events(
    execution_id: str,
    since_seq: int = Query(0, description="Return events with seq > since_seq"),
    limit: int = Query(500, le=1000),
    user=Depends(get_current_user),
) -> list[EventOut]:
    async with async_session() as session:
        stmt = (
            select(ExecutionEvent)
            .where(ExecutionEvent.execution_id == execution_id)
            .where(ExecutionEvent.seq > since_seq)
            .order_by(ExecutionEvent.seq)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            EventOut(
                event_id=r.event_id, event_type=r.event_type,
                org_id=r.org_id, app_id=r.app_id,
                conversation_id=r.conversation_id, message_id=r.message_id,
                execution_id=r.execution_id, node_run_id=r.node_run_id,
                seq=r.seq, data=r.data or {},
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]
```

**Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/execution_events.py backend/main.py
git commit -m "feat(api): GET /api/v1/executions/{id}/events PG replay endpoint"
```

---

### Task 7: FSM transition emit hooks (additive, FSM logic unchanged)

**Files:**
- Modify: `backend/app/services/synexia/fsm.py` (add `bus.publish` calls at transition points)

**Step 1: Find transition points**

The FSM is in `backend/app/services/synexia/fsm.py` (class `SynexiaFSM`, method `run` at line 79). Read it to identify the three transitions: `PENDING → RUNNING`, `RUNNING → COMPLETED`, `RUNNING → FAILED`.

**Step 2: Add emit calls**

In `SynexiaFSM.run()`, immediately after each transition write to the DB, add:

```python
from app.services.events import get_bus
from app.services.events.canonical_events import (
    execution_started, node_started, node_completed, node_failed,
    execution_completed, execution_failed,
)

# After FSMState transitions to RUNNING:
await get_bus().publish(execution_started(
    conversation_id=..., execution_id=self.execution.id,
    plan=current_plan, org_id=..., app_id=...,
))

# At each node start (tool/llm dispatch):
await get_bus().publish(node_started(
    conversation_id=..., execution_id=..., node_run_id=...,
    node_name=node_name, node_type=node_type,
))

# At each node completion / failure:
await get_bus().publish(node_completed(...))  # or node_failed(...)
```

**Step 3: Add a test**

Append to `backend/tests/test_canonical_events.py`:
```python
import pytest
from app.services.events.event_bus import EventBus, CanonicalEvent

async def test_bus_records_published_envelope_keys():
    bus = EventBus()
    env = CanonicalEvent(event_type="execution.started", conversation_id="c1",
                         execution_id="e1", data={"plan": []})
    d = env.to_dict()
    for k in ("event_id", "event_type", "conversation_id", "execution_id", "data", "created_at"):
        assert k in d
```

**Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/synexia/fsm.py backend/tests/test_canonical_events.py
git commit -m "feat(fsm): emit execution.* canonical events on FSM transitions"
```

---

### Task 8: `approval.required` emit on `ApprovalService.create()`

**Files:**
- Modify: `backend/app/services/governance/approval_service.py`

**Step 1: Find `create`/`create_request` method**

Read the file to identify where an `ApprovalRequest` row is inserted. Add after the row commit:

```python
from app.services.events import get_bus
from app.services.events.canonical_events import approval_required

await get_bus().publish(approval_required(
    conversation_id=req.conversation_id,
    execution_id=req.execution_id,
    approval_request_id=created.id,
    risk_tier=created.risk_tier,
    action=created.action,
    justification=created.justification or "",
    org_id=created.org_id, app_id=created.app_id,
))
```

**Step 2: Add a test**

```python
# backend/tests/test_approval_emits.py
import pytest

@pytest.mark.asyncio
async def test_approval_create_emits_required_event(monkeypatch):
    captured = []
    class FakeBus:
        async def publish(self, env, persist=True):
            captured.append(env)
    import app.services.governance.approval_service as mod
    monkeypatch.setattr(mod, "get_bus", lambda: FakeBus())
    # ... call ApprovalService.create(...) with required fields
    # Assert at least one captured env with event_type == "approval.required"
```

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/governance/approval_service.py backend/tests/test_approval_emits.py
git commit -m "feat(governance): emit approval.required canonical event on create"
```

---

## Phase 2 — Artifacts (validation + regenerate + preview extensions)

### Task 9: Artifact validation checks

**Files:**
- Create: `backend/app/services/artifacts/validation.py`
- Modify: `backend/app/services/artifacts/artifact_service.py` (call validation on `mark_version_built`)
- Modify: `backend/app/models/artifact.py` (add `validation_issues` JSON column)
- Create: `backend/alembic/versions/011_artifact_validation.py`
- Create: `backend/tests/test_artifact_validation.py`

**Step 1: Write the validator**

```python
# backend/app/services/artifacts/validation.py
"""Validation checks for built artifact versions.

Run after a sandbox job completes and before marking a version built.
Each check returns (passed: bool, issues: list[str]).
"""
from __future__ import annotations
import os
import zipfile
from typing import Callable

from app.models.artifact import ArtifactVersion

CHECKS: list[Callable[[ArtifactVersion, str], tuple[bool, list[str]]]] = []


def register(check):
    CHECKS.append(check)
    return check


@register
def check_file_exists(version: ArtifactVersion, blob_path: str) -> tuple[bool, list[str]]:
    if not os.path.exists(blob_path):
        return False, [f"File missing at {blob_path}"]
    if os.path.getsize(blob_path) == 0:
        return False, ["File is empty"]
    return True, []


@register
def check_zip_integrity(version: ArtifactVersion, blob_path: str) -> tuple[bool, list[str]]:
    if version.kind in {"pptx", "docx", "xlsx"}:
        try:
            with zipfile.ZipFile(blob_path) as zf:
                bad = zf.testzip()
                if bad:
                    return False, [f"Corrupt entry in zip: {bad}"]
        except zipfile.BadZipFile as e:
            return False, [f"Not a valid zip: {e}"]
    return True, []


@register
def check_size_limit(version: ArtifactVersion, blob_path: str) -> tuple[bool, list[str]]:
    LIMIT_MB = {"pptx": 200, "docx": 100, "xlsx": 200, "pdf": 200, "html": 50}
    cap = LIMIT_MB.get(version.kind, 500)
    size_mb = os.path.getsize(blob_path) / (1024 * 1024)
    if size_mb > cap:
        return False, [f"File {size_mb:.1f}MB exceeds {cap}MB cap for {version.kind}"]
    return True, []


def run_checks(version: ArtifactVersion, blob_path: str) -> tuple[bool, list[str]]:
    issues: list[str] = []
    for check in CHECKS:
        ok, found = check(version, blob_path)
        if not ok:
            issues.extend(found)
    return (len(issues) == 0, issues)
```

**Step 2: Add `validation_issues` column to `ArtifactVersion` model**

In `backend/app/models/artifact.py`, add to the `ArtifactVersion` class:
```python
validation_issues: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
```

Also extend `ARTIFACT_VERSION_STATUSES` with `"validation_failed"`.

**Step 3: Write the migration `backend/alembic/versions/011_artifact_validation.py`**

```python
"""artifact_versions.validation_issues"""
from alembic import op
import sqlalchemy as sa

revision = "011_artifact_validation"
down_revision = "010_execution_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifact_versions", sa.Column("validation_issues", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifact_versions", "validation_issues")
```

Run: `cd /root/zhanlu/backend && alembic upgrade head`

**Step 4: Hook validation into `mark_version_built`**

In `backend/app/services/artifacts/artifact_service.py`, find `mark_version_built` (or the path that sets `status="built"`). Right before promoting the version, add:

```python
from app.services.artifacts.validation import run_checks

ok, issues = run_checks(version, blob_path)
if not ok:
    version.status = "validation_failed"
    version.validation_issues = issues
    await session.commit()
    return {"ok": False, "issues": issues}
version.status = "built"
await session.commit()
return {"ok": True}
```

**Step 5: Test**

```python
# backend/tests/test_artifact_validation.py
import os, tempfile
from app.models.artifact import ArtifactVersion
from app.services.artifacts.validation import run_checks

def test_check_file_exists_detects_missing():
    v = ArtifactVersion(kind="pptx")
    ok, issues = run_checks(v, "/no/such/file")
    assert not ok
    assert any("missing" in i.lower() for i in issues)

def test_check_size_limit_caps_pptx():
    with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as f:
        f.write(b"x" * (210 * 1024 * 1024))
        path = f.name
    try:
        v = ArtifactVersion(kind="pptx")
        ok, issues = run_checks(v, path)
        assert not ok
        assert any("cap" in i.lower() for i in issues)
    finally:
        os.unlink(path)
```

Run: `cd /root/zhanlu/backend && pytest tests/test_artifact_validation.py -v`
Expected: PASS.

**Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/artifacts/validation.py backend/app/services/artifacts/artifact_service.py backend/app/models/artifact.py backend/alembic/versions/011_artifact_validation.py backend/tests/test_artifact_validation.py
git commit -m "feat(artifacts): validation checks runner + hook on mark_version_built"
```

---

### Task 10: `POST /api/v1/artifacts/{id}/regenerate`

**Files:**
- Create: `backend/app/services/artifacts/regenerate.py`
- Modify: `backend/app/routers/artifacts.py` (add endpoint)
- Create: `backend/tests/test_artifact_regenerate.py`

**Step 1: Write the service**

```python
# backend/app/services/artifacts/regenerate.py
"""Regenerate a new version of an existing artifact.

Re-runs the original sandbox job spec and produces a new ArtifactVersion
linked to the same artifact. Old versions remain intact.
"""
from __future__ import annotations
import logging
import uuid
from sqlalchemy import select

from app.models.artifact import Artifact, ArtifactVersion
from app.models.sandbox_job import SandboxJob
from app.services.db import async_session

logger = logging.getLogger(__name__)


async def regenerate_artifact(artifact_id: str, user_id: str) -> str:
    async with async_session() as session:
        artifact = (await session.execute(
            select(Artifact).where(Artifact.id == artifact_id)
        )).scalar_one_or_none()
        if artifact is None:
            raise ValueError(f"Artifact {artifact_id} not found")

        latest = (await session.execute(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version.desc())
            .limit(1)
        )).scalar_one_or_none()
        if latest is None:
            raise ValueError(f"Artifact {artifact_id} has no versions to regenerate from")

        job = SandboxJob(
            id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            artifact_version_id=None,
            conversation_id=artifact.conversation_id,
            execution_id=artifact.execution_id,
            skill_name=latest.skill_name or "unknown",
            skill_version=latest.skill_version,
            input_package=latest.input_package or {},
            output_spec=latest.output_spec or {},
            status="queued",
            timeout_seconds=latest.timeout_seconds or 120,
        )
        session.add(job)
        await session.commit()
        return job.id
```

**Step 2: Add the endpoint**

Append to `backend/app/routers/artifacts.py`:

```python
from app.services.artifacts.regenerate import regenerate_artifact

@router.post("/artifacts/{artifact_id}/regenerate")
async def regenerate(artifact_id: str, user=Depends(get_current_user)):
    job_id = await regenerate_artifact(artifact_id, user.id)
    return {"sandbox_job_id": job_id, "status": "queued"}
```

**Step 3: Test**

```python
# backend/tests/test_artifact_regenerate.py
import pytest
from unittest.mock import patch, AsyncMock
from app.services.artifacts.regenerate import regenerate_artifact

@pytest.mark.asyncio
async def test_regenerate_creates_new_job():
    with patch("app.services.artifacts.regenerate.async_session") as m:
        # mock session.execute to return an Artifact, then a latest ArtifactVersion
        # mock session.commit
        job_id = await regenerate_artifact("a1", "u1")
        assert job_id is not None
```

**Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/artifacts/regenerate.py backend/app/routers/artifacts.py backend/tests/test_artifact_regenerate.py
git commit -m "feat(artifacts): POST /artifacts/{id}/regenerate endpoint"
```

---

### Task 11: DOCX→PDF in `preview_builder.py`

**Files:**
- Modify: `backend/app/services/artifacts/preview_builder.py`
- Create: `backend/tests/test_docx_preview.py`

**Step 1: Add DOCX branch**

Find the dispatcher in `preview_builder.py` (likely `build_preview(version, blob_path)` with an if/elif on `version.kind`). Add a DOCX branch mirroring the existing PPTX/XLSX LibreOffice conversion:

```python
elif kind == "docx":
    import subprocess, tempfile
    out_dir = tempfile.mkdtemp()
    subprocess.run([
        "libreoffice", "--headless",
        "--convert-to", "pdf", "--outdir", out_dir, blob_path,
    ], check=True, capture_output=True, timeout=120)
    base = os.path.splitext(os.path.basename(blob_path))[0]
    pdf_path = os.path.join(out_dir, base + ".pdf")
    return pdf_path
```

**Step 2: Test**

```python
# backend/tests/test_docx_preview.py
import os, subprocess
from app.services.artifacts.preview_builder import build_preview

def test_docx_preview_calls_libreoffice(tmp_path, monkeypatch):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        out_dir = cmd[cmd.index("--outdir") + 1]
        # create a fake pdf
        with open(os.path.join(out_dir, "out.pdf"), "w") as f:
            f.write("%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    fake_docx = tmp_path / "in.docx"
    fake_docx.write_bytes(b"PK\x03\x04fake")
    from app.models.artifact import ArtifactVersion
    v = ArtifactVersion(kind="docx")
    out = build_preview(v, str(fake_docx))
    assert out.endswith(".pdf")
    assert calls and "libreoffice" in calls[0]
```

Run: `cd /root/zhanlu/backend && pytest tests/test_docx_preview.py -v`
Expected: PASS.

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/artifacts/preview_builder.py backend/tests/test_docx_preview.py
git commit -m "feat(artifacts): DOCX -> PDF preview via LibreOffice"
```

---

### Task 12: Artifact create + preview_ready canonical emissions

**Files:**
- Modify: `backend/app/services/artifacts/artifact_service.py`

**Step 1: Add emissions**

In the artifact version create path (immediately after the new `ArtifactVersion` row is committed), add:

```python
from app.services.events import get_bus
from app.services.events.canonical_events import artifact_created, artifact_preview_ready

await get_bus().publish(artifact_created(
    conversation_id=artifact.conversation_id,
    execution_id=artifact.execution_id,
    artifact_id=artifact.id, artifact_version_id=version.id, kind=version.kind,
    org_id=artifact.org_id, app_id=artifact.app_id,
))
```

In the preview-ready path (after `preview_path` is stored on the version), add:

```python
await get_bus().publish(artifact_preview_ready(
    conversation_id=artifact.conversation_id, execution_id=artifact.execution_id,
    artifact_id=artifact.id, artifact_version_id=version.id,
    preview_url=f"/api/v1/artifacts/{artifact.id}/versions/{version.id}/preview",
    org_id=artifact.org_id, app_id=artifact.app_id,
))
```

**Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/artifacts/artifact_service.py
git commit -m "feat(artifacts): emit artifact.created + artifact.preview_ready canonical events"
```

---

### Task 13: Download permission check on artifact download endpoint

**Files:**
- Modify: `backend/app/routers/artifacts.py` (add guard)
- Modify: `backend/app/services/permissions/__init__.py` (add `can_download_artifact`)

**Step 1: Add `can_download_artifact` helper**

In `backend/app/services/permissions/__init__.py`:

```python
async def can_download_artifact(user, version) -> bool:
    """Org-scoped permission check for downloading an artifact version.

    Rules:
      - Same org -> allowed.
      - Public artifacts (no org) -> allowed.
      - Different org -> denied.
    """
    artifact = version.artifact
    if user.org_id and artifact.org_id and user.org_id != artifact.org_id:
        return False
    return True
```

**Step 2: Add the guard in the download endpoint**

Find the existing `GET /artifacts/{artifact_id}/versions/{version_id}/download` handler. Wrap the body with:

```python
from app.services.permissions import can_download_artifact

@router.get("/artifacts/{artifact_id}/versions/{version_id}/download")
async def download(artifact_id: str, version_id: str, user=Depends(get_current_user)):
    version = await get_artifact_version(artifact_id, version_id)
    if not await can_download_artifact(user, version):
        raise HTTPException(403, "No download permission for this artifact")
    return FileResponse(version.blob_path, filename=version.filename)
```

**Step 3: Test**

```python
# backend/tests/test_artifact_download_permission.py
import pytest
from app.services.permissions import can_download_artifact

class U:
    def __init__(self, org_id): self.org_id = org_id
class V:
    def __init__(self, org_id):
        self.artifact = type("A", (), {"org_id": org_id})()

@pytest.mark.asyncio
async def test_same_org_allowed():
    assert await can_download_artifact(U("o1"), V("o1")) is True

@pytest.mark.asyncio
async def test_different_org_denied():
    assert await can_download_artifact(U("o1"), V("o2")) is False
```

Run: `cd /root/zhanlu/backend && pytest tests/test_artifact_download_permission.py -v`
Expected: PASS.

**Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/artifacts.py backend/app/services/permissions/__init__.py backend/tests/test_artifact_download_permission.py
git commit -m "feat(artifacts): download permission check on artifact version endpoint"
```

---

## Phase 3 — Sandbox Hardening

### Task 14: Add `network_policy`, `cpu_limit`, `memory_limit_mb` columns to `sandbox_jobs`

**Files:**
- Modify: `backend/app/models/sandbox_job.py`
- Create: `backend/alembic/versions/012_sandbox_resource_limits.py`

**Step 1: Add the columns**

In `backend/app/models/sandbox_job.py`, add to `class SandboxJob`:

```python
# Resource limits (per-job overrides; default to safe values in worker)
network_policy: Mapped[Optional[str]] = mapped_column(String(20), default="none", nullable=True)  # none|outbound|isolated
cpu_limit: Mapped[Optional[float]] = mapped_column(nullable=True)        # e.g. 1.5 (cores)
memory_limit_mb: Mapped[Optional[int]] = mapped_column(nullable=True)    # e.g. 1024
```

**Step 2: Migration `backend/alembic/versions/012_sandbox_resource_limits.py`**

```python
"""sandbox_jobs resource limits"""
from alembic import op
import sqlalchemy as sa

revision = "012_sandbox_resource_limits"
down_revision = "011_artifact_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sandbox_jobs", sa.Column("network_policy", sa.String(20), nullable=True, server_default="none"))
    op.add_column("sandbox_jobs", sa.Column("cpu_limit", sa.Float(), nullable=True))
    op.add_column("sandbox_jobs", sa.Column("memory_limit_mb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sandbox_jobs", "memory_limit_mb")
    op.drop_column("sandbox_jobs", "cpu_limit")
    op.drop_column("sandbox_jobs", "network_policy")
```

Run: `cd /root/zhanlu/backend && alembic upgrade head`

**Step 3: Update the sandbox router to accept the fields**

In `backend/app/routers/sandbox.py`, find the `CreateJobRequest` (or equivalent) and add the three optional fields.

**Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/app/models/sandbox_job.py backend/alembic/versions/012_sandbox_resource_limits.py backend/app/routers/sandbox.py
git commit -m "feat(sandbox): per-job network_policy, cpu_limit, memory_limit_mb"
```

---

### Task 15: Sandbox-worker publishes thin canonical events

**Files:**
- Modify: `backend/sandbox_worker/main.py`

**Step 1: Add bus publish at each lifecycle point**

In `sandbox_worker/main.py`, find the four lifecycle points (job started, command started, command completed, job completed/failed/timeout). After writing to `sandbox_job_events` at each point, add a thin `bus.publish(...)` call. Do **not** include the full stdout/stderr body in the canonical event — that lives in `sandbox_job_events` only.

```python
from app.services.events import get_bus
from app.services.events.canonical_events import (
    node_started, node_completed, node_failed,
    execution_completed, execution_failed,
)

# At job started (sandbox job == a node in an execution):
await get_bus().publish(node_started(
    conversation_id=job.conversation_id, execution_id=job.execution_id,
    node_run_id=job.id, node_name=f"sandbox:{job.skill_name}", node_type="sandbox",
    org_id=None, app_id=None,
))

# At command completed:
await get_bus().publish(node_completed(
    conversation_id=job.conversation_id, execution_id=job.execution_id,
    node_run_id=job.id, result={"command_seq": cmd.seq, "exit_code": cmd.exit_code},
))

# At job completed / failed / timeout:
await get_bus().publish(execution_completed(...) if status == "completed" else execution_failed(...))
```

(The bus is async; wrap these in `asyncio.run_coroutine_threadsafe` or call them via the existing async event loop in the worker. The worker's `main.py` already runs an event loop — make `publish` calls awaited inside the async paths. If a path is sync, schedule with `asyncio.get_event_loop().create_task`.)

**Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/sandbox_worker/main.py
git commit -m "feat(sandbox-worker): publish thin canonical events at lifecycle points"
```

---

### Task 16: Sandbox recovery on worker startup

**Files:**
- Create: `backend/sandbox_worker/recovery.py`
- Create: `backend/tests/test_sandbox_recovery.py`
- Modify: `backend/sandbox_worker/main.py` (call recovery on startup)

**Step 1: Write the recovery module**

```python
# backend/sandbox_worker/recovery.py
"""Scan for orphaned sandbox jobs and reconcile.

On worker startup, find all jobs with status in (queued, running) that have
not had a heartbeat in HEARTBEAT_TIMEOUT_SECS, mark them failed, and
emit a canonical execution.failed event.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select, update

from app.models.sandbox_job import SandboxJob, SANDBOX_JOB_STATUSES
from app.services.db import async_session
from app.services.events import get_bus
from app.services.events.canonical_events import node_failed, execution_failed

logger = logging.getLogger(__name__)

HEARTBEAT_TIMEOUT_SECS = 300  # 5 minutes


async def recover_orphaned_jobs() -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECS)
    recovered = 0
    async with async_session() as session:
        stmt = (
            select(SandboxJob)
            .where(SandboxJob.status.in_(["queued", "running"]))
            .where(SandboxJob.updated_at < cutoff)
        )
        rows = (await session.execute(stmt)).scalars().all()
        for job in rows:
            logger.warning("Recovering orphaned sandbox job %s (status=%s)", job.id, job.status)
            job.status = "failed"
            job.error_message = "Recovered on worker startup: no heartbeat"
            await session.commit()
            if job.conversation_id and job.execution_id:
                await get_bus().publish(node_failed(
                    conversation_id=job.conversation_id,
                    execution_id=job.execution_id,
                    node_run_id=job.id,
                    error="sandbox worker did not heartbeat",
                ))
            recovered += 1
    return recovered


async def run_recovery_on_startup() -> None:
    n = await recover_orphaned_jobs()
    logger.info("Sandbox recovery: %d orphaned job(s) reconciled", n)
```

**Step 2: Call from worker startup**

In `sandbox_worker/main.py`, at the top of `main()` or the lifespan handler:

```python
from sandbox_worker.recovery import run_recovery_on_startup
await run_recovery_on_startup()
```

**Step 3: Test**

```python
# backend/tests/test_sandbox_recovery.py
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock
from app.models.sandbox_job import SandboxJob

@pytest.mark.asyncio
async def test_recover_orphaned_jobs_marks_failed():
    with patch("app.services.db.async_session") as m:
        # mock session to return a single orphaned job
        m.return_value.__aenter__.return_value.execute.return_value.scalars.return_value.all.return_value = [
            SandboxJob(id="j1", status="running", conversation_id="c1", execution_id="e1")
        ]
        from app.sandbox_worker.recovery import recover_orphaned_jobs
        n = await recover_orphaned_jobs()
        assert n == 1
```

Run: `cd /root/zhanlu/backend && pytest tests/test_sandbox_recovery.py -v`
Expected: PASS.

**Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/sandbox_worker/recovery.py backend/sandbox_worker/main.py backend/tests/test_sandbox_recovery.py
git commit -m "feat(sandbox): worker startup recovery for orphaned jobs"
```

---

### Task 17: HTML preview served inside sandboxed `<iframe>`

**Files:**
- Modify: `backend/app/routers/artifacts.py` (HTML preview endpoint)

**Step 1: Add the wrapper endpoint**

The existing preview endpoint serves a `FileResponse`. For `kind == "html"`, instead serve a small HTML wrapper page with a sandboxed iframe:

```python
@router.get("/artifacts/{artifact_id}/versions/{version_id}/preview")
async def preview(artifact_id: str, version_id: str, user=Depends(get_current_user)):
    version = await get_artifact_version(artifact_id, version_id)
    if not await can_download_artifact(user, version):
        raise HTTPException(403, "No preview permission for this artifact")
    if version.kind == "html":
        wrapper = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{version.filename}</title>
<style>html,body,iframe{{margin:0;padding:0;width:100%;height:100%;border:0}}</style>
</head><body>
<iframe src="/api/v1/artifacts/{artifact_id}/versions/{version_id}/raw"
        sandbox="allow-scripts allow-same-origin"
        referrerpolicy="no-referrer"></iframe>
</body></html>"""
        return HTMLResponse(wrapper)
    # Non-HTML: serve a PDF or existing preview
    return FileResponse(version.preview_path)
```

Add a `GET /artifacts/{artifact_id}/versions/{version_id}/raw` endpoint that returns the underlying HTML file with `Content-Type: text/html`.

**Step 2: Test**

```python
# backend/tests/test_html_iframe_wrapper.py
import pytest
from app.routers.artifacts import preview  # async handler

@pytest.mark.asyncio
async def test_html_preview_returns_wrapper():
    # mock version with kind=html, blob_path=tmp html
    resp = await preview("a1", "v1", user=...)
    body = resp.body.decode()
    assert "<iframe" in body
    assert 'sandbox="allow-scripts allow-same-origin"' in body
```

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/artifacts.py backend/tests/test_html_iframe_wrapper.py
git commit -m "feat(artifacts): HTML preview served inside sandboxed iframe"
```

---

### Task 18: Final acceptance test

**Files:**
- Create: `backend/tests/test_end_to_end_canonical.py`

**Step 1: End-to-end test**

```python
# backend/tests/test_end_to_end_canonical.py
"""End-to-end smoke test of the canonical event stream."""
import asyncio
import pytest
from app.services.events import get_bus
from app.services.events.canonical_events import (
    message_created, execution_started, node_started,
    node_completed, execution_completed, artifact_created, artifact_preview_ready,
)

@pytest.mark.asyncio
async def test_full_timeline_round_trip():
    bus = get_bus()
    cid = "e2e-conv-1"
    q = bus.subscribe(cid)

    events_in = [
        message_created(conversation_id=cid, message_id="m1", role="user"),
        execution_started(conversation_id=cid, execution_id="e1", plan=[]),
        node_started(conversation_id=cid, execution_id="e1", node_run_id="n1",
                     node_name="respond", node_type="llm"),
        node_completed(conversation_id=cid, execution_id="e1", node_run_id="n1", result={}),
        execution_completed(conversation_id=cid, execution_id="e1", summary="ok"),
        artifact_created(conversation_id=cid, execution_id="e1",
                         artifact_id="a1", artifact_version_id="v1", kind="pptx"),
        artifact_preview_ready(conversation_id=cid, execution_id="e1",
                               artifact_id="a1", artifact_version_id="v1",
                               preview_url="/preview"),
    ]
    for ev in events_in:
        await bus.publish(ev, persist=False)

    seen = []
    for _ in range(len(events_in)):
        seen.append(await asyncio.wait_for(q.get(), timeout=1.0))

    types = [e["event_type"] for e in seen]
    assert types == [
        "message.created", "execution.started", "execution.node_started",
        "execution.node_completed", "execution.completed",
        "artifact.created", "artifact.preview_ready",
    ]
```

Run: `cd /root/zhanlu/backend && pytest tests/test_end_to_end_canonical.py -v`
Expected: PASS.

**Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/tests/test_end_to_end_canonical.py
git commit -m "test(events): end-to-end smoke test of canonical timeline"
```

---

## Cross-cutting summary

### New files (full list)

- `backend/app/models/execution_event.py`
- `backend/app/services/events/__init__.py`
- `backend/app/services/events/event_bus.py`
- `backend/app/services/events/canonical_events.py`
- `backend/app/services/agent_runtime/__init__.py`
- `backend/app/services/agent_runtime/canonical_runner.py`
- `backend/app/services/artifacts/validation.py`
- `backend/app/services/artifacts/regenerate.py`
- `backend/app/routers/chat_stream.py`
- `backend/app/routers/execution_events.py`
- `backend/sandbox_worker/recovery.py`
- `backend/tests/test_canonical_events.py`
- `backend/tests/test_approval_emits.py`
- `backend/tests/test_artifact_validation.py`
- `backend/tests/test_artifact_regenerate.py`
- `backend/tests/test_docx_preview.py`
- `backend/tests/test_artifact_download_permission.py`
- `backend/tests/test_sandbox_recovery.py`
- `backend/tests/test_html_iframe_wrapper.py`
- `backend/tests/test_end_to_end_canonical.py`
- `backend/alembic/versions/010_execution_events.py`
- `backend/alembic/versions/011_artifact_validation.py`
- `backend/alembic/versions/012_sandbox_resource_limits.py`

### Modified files (additive only)

- `backend/app/models/__init__.py` (export new model)
- `backend/app/models/artifact.py` (add `validation_issues` column)
- `backend/app/models/sandbox_job.py` (add `network_policy`, `cpu_limit`, `memory_limit_mb`)
- `backend/app/routers/agents.py` (untouched)
- `backend/app/routers/artifacts.py` (regenerate, download guard, HTML wrapper)
- `backend/app/routers/sandbox.py` (accept new job fields)
- `backend/app/services/synexia/fsm.py` (emit `execution.*` events on transitions)
- `backend/app/services/governance/approval_service.py` (emit `approval.required`)
- `backend/app/services/artifacts/artifact_service.py` (call validation, emit artifact events)
- `backend/app/services/artifacts/preview_builder.py` (DOCX branch)
- `backend/app/services/permissions/__init__.py` (add `can_download_artifact`)
- `backend/sandbox_worker/main.py` (bus publish + recovery on startup)
- `backend/main.py` (register 3 new routers)

### Explicitly NOT changed

- `backend/app/routers/agents.py` (v3 SSE endpoint is **untouched**)
- `sandbox_job_events` table (kept as raw audit log; no schema change)
- `ApprovalRequest` row writes (kept as-is; we only add a side-effect emit)
- v3 event shape (`{type: delta|done|error|paused|tool_progress}`)

### Verification checklist (run before marking done)

- [ ] All 20 test files pass: `cd backend && pytest -v`
- [ ] All 3 migrations apply cleanly: `alembic upgrade head`
- [ ] `POST /api/v1/chat/stream` returns SSE with canonical envelopes: `curl -N -X POST .../api/v1/chat/stream -d '{...}'`
- [ ] `GET /api/v1/executions/{id}/events` returns the persisted timeline
- [ ] v3 endpoint `POST /api/v1/apps/{app_id}/agents/conversations/v3/{cid}/messages/stream` still works (no behavior change)
- [ ] Sandbox worker starts, runs `run_recovery_on_startup`, picks up jobs
- [ ] Artifact validation blocks a corrupt blob from being marked `built`
- [ ] HTML preview returns a wrapper iframe, not raw HTML at the preview URL
- [ ] Download permission check returns 403 for cross-org users

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-07-13-canonical-events-artifacts-sandbox.md`. Two execution options:

1. **Subagent-Driven (this session)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Parallel Session (separate)** — Open a new session with `executing-plans`, batch execution with checkpoints.

Which approach?
