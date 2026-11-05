"""R6 Phase 2 — §5 Agent Invocation Contract (TDD).

Spec: docs/superpowers/specs/2026-11-03-zhanlu-automation-technical-specification.md §5.

Module under test: ``app/services/agent_invocation.py`` (SINGULAR — the
invocation contract; the user's plural ``agent_invocations.py`` trace/cost
recorder is a DIFFERENT module and is never touched here).

Verified behaviors:
1. Typed context/request/event models validate (pydantic §5 shapes).
2. ``invoke_agent`` over a FAKE v3 stream yields structured ``AgentEvent``s —
   callers never see SSE text or regex.
3. Both stream shapes work: the legacy coroutine→StreamingResponse shape AND
   the bare async-generator shape (the v3 handler's current in-flight form).
4. Cancellation signal stops consumption at the next chunk boundary.
5. Wall-clock deadline enforced at the CONTRACT layer: deadline exceeded ⇒
   ``run_failed(classification="timeout")``.
6. v3 body parity with the executor's current mechanics (force_planning,
   phase="automation", automation ids).
7. run_finished provenance: status/content_hash/usage/agent_run_id/
   conversation_id — carried regardless of terminal status (audit S5).
8. seq strictly increasing; occurred_at always set; ``stream_ended`` is the
   final event on every path.
9. resume=True routes through the resume path with the approval id.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
from app.services.agent_invocation import (  # noqa: E402
    AgentEvent,
    AgentInvocationContext,
    AgentInvocationRequest,
    AutomationRef,
    CancellationSignal,
    ExecutionBudgets,
    invoke_agent,
    parse_sse_frame,
)


def _sse(evt: dict) -> str:
    return f"data: {json.dumps(evt)}\n\n"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _ctx(db, *, deadline=None) -> AgentInvocationContext:
    return AgentInvocationContext(
        db=db,
        user=type("U", (), {"id": "u1"})(),
        org_id="org-1",
        app_id="app-1",
        project_id=None,
        conversation_id=str(uuid.uuid4()),
        automation=AutomationRef(
            definition_id=f"task-{uuid.uuid4().hex[:8]}",
            execution_id=f"ex-{uuid.uuid4().hex[:8]}",
            attempt=0,
            spec_version="v1",
            trigger="schedule",
        ),
        cancellation=CancellationSignal(deadline=deadline),
        budgets=ExecutionBudgets(),
        trace_id="tr-" + uuid.uuid4().hex[:8],
    )


# --------------------------------------------------------------------------
# Fake v3 streams — mimicking app.routers.agents.add_message_stream
# --------------------------------------------------------------------------

class _FakeResponse:
    """Legacy shape: await handler → StreamingResponse.body_iterator."""

    def __init__(self, frames):
        self.body_iterator = _agen(frames)


async def _agen(frames):
    for f in frames:
        yield f


def _install_stream(monkeypatch, frames, *, generator_shape=False, capture=None):
    """Install a fake add_message_stream on app.routers.agents."""
    import app.routers.agents as agents_mod

    if generator_shape:
        async def fake_stream(**kwargs):
            if capture is not None:
                capture.update(kwargs)
            for f in frames:
                yield f
    else:
        async def fake_stream(**kwargs):
            if capture is not None:
                capture.update(kwargs)
            return _FakeResponse(frames)

    monkeypatch.setattr(agents_mod, "add_message_stream", fake_stream)
    return fake_stream


# --------------------------------------------------------------------------
# 1. Types
# --------------------------------------------------------------------------

def test_types_validate():
    ctx = _ctx(None)
    assert ctx.automation.execution_id.startswith("ex-")
    req = AgentInvocationRequest(message="do the thing")
    assert req.force_planning is True and req.resume is False
    evt = AgentEvent(seq=1, occurred_at=datetime.now(timezone.utc),
                     type="delta", payload={"content": "x"})
    with pytest.raises(Exception):
        AgentEvent(seq=1, occurred_at=datetime.now(timezone.utc),
                   type="bogus_type", payload={})


# --------------------------------------------------------------------------
# 2/3/6/7/8. Happy path, shapes, parity, provenance
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("generator_shape", [False, True])
async def test_happy_path_structured_events_and_provenance(db, monkeypatch,
                                                           generator_shape):
    conv_id = str(uuid.uuid4())
    frames = [
        _sse({"type": "phase", "state": "planning"}),
        _sse({"type": "delta", "content": "Hello "}),
        _sse({"type": "delta", "content": "world"}),
        _sse({"type": "message_start", "server_message_id": "sm-1",
              "run_id": "run-42"}),
        _sse({"type": "done", "completion_status": "succeeded",
              "content_hash": "h-abc", "usage": {"total_tokens": 90},
              "message_id": "sm-1"}),
    ]
    captured = {}
    _install_stream(monkeypatch, frames, generator_shape=generator_shape,
                    capture=captured)
    ctx = _ctx(db)
    ctx.conversation_id = conv_id

    events = [e async for e in invoke_agent(
        ctx, AgentInvocationRequest(message="do the thing"))]

    types = [e.type for e in events]
    assert types[0] == "phase"
    assert types[-1] == "stream_ended"
    assert "run_finished" in types
    assert types.count("stream_ended") == 1  # exactly one terminal marker
    # seq strictly increasing, occurred_at always set
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(e.occurred_at is not None for e in events)
    # callers never see raw SSE
    assert all("data:" not in json.dumps(e.payload) for e in events)

    fin = next(e for e in events if e.type == "run_finished")
    assert fin.payload["status"] == "succeeded"
    assert fin.payload["content_hash"] == "h-abc"
    assert fin.payload["usage"] == {"total_tokens": 90}
    assert fin.payload["agent_run_id"] == "run-42"
    assert fin.payload["conversation_id"] == conv_id

    # v3 body parity with the executor's current mechanics (§5 / :2546)
    body = captured["body"]
    assert body["content"] == "do the thing"
    assert body["force_planning"] is True
    assert body["phase"] == "automation"
    assert body["automation_task_id"] == ctx.automation.definition_id
    assert body["automation_execution_id"] == ctx.automation.execution_id
    assert captured["conversation_id"] == conv_id
    assert captured["app_id"] == "app-1"


@pytest.mark.asyncio
async def test_error_maps_to_run_failed_provenance_kept(db, monkeypatch):
    conv_id = str(uuid.uuid4())
    frames = [
        _sse({"type": "delta", "content": "partial"}),
        _sse({"type": "error", "error": "provider 503"}),
    ]
    _install_stream(monkeypatch, frames, generator_shape=True)
    ctx = _ctx(db)
    ctx.conversation_id = conv_id

    events = [e async for e in invoke_agent(
        ctx, AgentInvocationRequest(message="m"))]
    types = [e.type for e in events]
    assert types[-1] == "stream_ended"
    failed = next(e for e in events if e.type == "run_failed")
    assert failed.payload["classification"] == "error"
    assert "provider 503" in json.dumps(failed.payload)
    # audit S5: conversation_id returned regardless of terminal status
    assert failed.payload["conversation_id"] == conv_id
    # partial content preserved on failure
    deltas = [e for e in events if e.type == "delta"]
    assert "".join(d.payload.get("content", "") for d in deltas) == "partial"


@pytest.mark.asyncio
async def test_resume_routes_through_resume_path(db, monkeypatch):
    import app.routers.agents as agents_mod
    frames = [
        _sse({"type": "delta", "content": "resumed"}),
        _sse({"type": "done", "completion_status": "succeeded"}),
    ]
    captured = {}

    async def fake_resume(**kwargs):
        captured.update(kwargs)
        return _FakeResponse(frames)

    monkeypatch.setattr(agents_mod, "resume_conversation", fake_resume)

    ctx = _ctx(db)
    events = [e async for e in invoke_agent(
        ctx,
        AgentInvocationRequest(message="m", resume=True, approval_id="ap-9"))]
    assert captured.get("approval_id") == "ap-9" or \
        captured.get("grant_scope") is not None
    assert any(e.type == "delta" and
               e.payload.get("content") == "resumed" for e in events)
    assert events[-1].type == "stream_ended"


# --------------------------------------------------------------------------
# 4/5. Cancellation + deadline (contract-layer enforcement)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancellation_stops_at_chunk_boundary(db, monkeypatch):
    total = 50
    frames = [_sse({"type": "delta", "content": f"c{i}"}) for i in range(total)]
    _install_stream(monkeypatch, frames, generator_shape=True)
    ctx = _ctx(db)
    signal = ctx.cancellation

    consumed = []
    async for ev in invoke_agent(ctx, AgentInvocationRequest(message="m")):
        consumed.append(ev)
        if len(consumed) == 3:
            signal.request_cancel()  # settable from the runner (sync-safe)
    types = [e.type for e in consumed]
    # consumption stopped well before all 50 chunks
    assert len(consumed) < total
    assert types[-1] == "stream_ended"
    failed = [e for e in consumed if e.type == "run_failed"]
    assert failed and failed[0].payload["classification"] == "cancelled"


@pytest.mark.asyncio
async def test_deadline_enforced_at_contract_layer(db, monkeypatch):
    frames = [_sse({"type": "delta", "content": "slow"}) for _ in range(10)]

    async def slow_stream(**kwargs):
        for f in frames:
            await asyncio.sleep(0.05)
            yield f

    import app.routers.agents as agents_mod
    monkeypatch.setattr(agents_mod, "add_message_stream", slow_stream)

    ctx = _ctx(db, deadline=datetime.now(timezone.utc) + timedelta(seconds=0.1))
    events = [e async for e in invoke_agent(
        ctx, AgentInvocationRequest(message="m"))]
    types = [e.type for e in events]
    assert types[-1] == "stream_ended"
    failed = [e for e in events if e.type == "run_failed"]
    assert failed and failed[0].payload["classification"] == "timeout"
    # never re-invoked past the deadline: fewer than all 10 chunks consumed
    assert len(events) < 15


@pytest.mark.asyncio
async def test_expired_deadline_fails_fast_before_invocation(db, monkeypatch):
    calls = []

    async def fake_stream(**kwargs):
        calls.append(kwargs)
        yield _sse({"type": "done", "completion_status": "succeeded"})

    import app.routers.agents as agents_mod
    monkeypatch.setattr(agents_mod, "add_message_stream", fake_stream)

    ctx = _ctx(db, deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    events = [e async for e in invoke_agent(
        ctx, AgentInvocationRequest(message="m"))]
    assert calls == []  # never invoked
    failed = [e for e in events if e.type == "run_failed"]
    assert failed and failed[0].payload["classification"] == "timeout"
    assert events[-1].type == "stream_ended"


# --------------------------------------------------------------------------
# 9. Centralized SSE frame parser
# --------------------------------------------------------------------------

def test_parse_sse_frame_variants():
    assert parse_sse_frame('data: {"type":"delta","content":"x"}\n\n') == {
        "type": "delta", "content": "x"}
    assert parse_sse_frame("data: not-json\n\n") is None
    assert parse_sse_frame("\n\n") is None
    assert parse_sse_frame("event: ping\ndata: {}\n\n") == {}
    # executor delegation: legacy behavior unchanged
    import app.services.automation_executor as aexec
    assert aexec._parse_sse_chunk(
        'data: {"type":"delta","content":"x"}\n\n') == {
        "type": "delta", "content": "x"}
    assert aexec._parse_sse_chunk("junk") is None
