"""Tests for skip_confirmation auto-proceed behavior (Phase 4, Task 1).

Covers the three pause-handling rules:
  * approval pause + skip_confirmation=true  -> auto-approve + resume (bounded)
  * decision-summary pause                   -> never auto-skipped (fails fast)
  * approval pause + skip_confirmation=false -> fails fast (legacy behavior)

The approve-then-resume logic lives in the module-level ``_approve_and_resume``
helper so it can be unit-tested without spinning up the full agent loop.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def _stream_chunks(events):
    """Build a list of SSE 'data: {json}' strings from event dicts."""
    return [f"data: {json.dumps(e)}" for e in events]


class _FakeConv:
    """Stand-in for AgentConversation that avoids DB persistence."""

    def __init__(self, **kw):
        self.id = kw.get("id", "conv-1")
        self.agent_name = kw.get("agent_name", "R")
        self.title = kw.get("title", "t")
        self.messages = list(kw.get("messages", []))
        self.status = kw.get("status", "active")
        self.org_id = kw.get("org_id", "o")
        self.app_id = kw.get("app_id", "a")
        self.created_by_id = kw.get("created_by_id", "u")
        self.metadata_ = dict(kw.get("metadata_", {}) or {})


def _make_task(skip="true"):
    task = MagicMock()
    task.id = "t1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"; task.name = "Daily Report"
    task.skip_confirmation = skip; task.session_id = None
    return task


# -- _approve_and_resume unit tests ----------------------------------------

def test_approve_and_resume_approves_then_resumes():
    """Helper reads approval_id from conv metadata, approves it, then resumes."""
    conv = _FakeConv(metadata_={
        "_resume_state": {"pending_tool": {"approval_id": "appr-1"}},
    })
    db = MagicMock()  # refresh is a no-op
    task = _make_task()
    loop = asyncio.new_event_loop()

    approved = {"called": False}

    def fake_approve(request_id, reviewed_by, notes=None):
        approved["called"] = True
        approved["request_id"] = request_id
        return SimpleNamespace(status="approved")

    resumed = {"status": "active"}

    async def fake_resume(*, app_id, conversation_id, db, user):
        resumed["called"] = True
        return {"status": "active"}

    with patch("app.services.governance.approval_service.ApprovalService") as ASvc, \
         patch("app.routers.agents.resume_conversation", new=AsyncMock(side_effect=fake_resume)):
        ASvc.return_value.approve = fake_approve
        result = ax._approve_and_resume(loop, db, conv, task)

    loop.close()
    assert approved["called"] is True
    assert approved["request_id"] == "appr-1"
    assert result == {"status": "active"}


def test_approve_and_resume_no_approval_id_still_resumes():
    """When approval_id is missing, the helper skips approve but still resumes
    (resume will feed a denial to the LLM — graceful degradation)."""
    conv = _FakeConv(metadata_={
        "_resume_state": {"pending_tool": {}},  # no approval_id
    })
    db = MagicMock()
    task = _make_task()
    loop = asyncio.new_event_loop()

    with patch("app.services.governance.approval_service.ApprovalService") as ASvc, \
         patch("app.routers.agents.resume_conversation", new=AsyncMock(return_value={"status": "active"})) as fres:
        ASvc.return_value.approve = MagicMock(side_effect=AssertionError("should not approve"))
        result = ax._approve_and_resume(loop, db, conv, task)
    loop.close()
    assert result == {"status": "active"}
    ASvc.return_value.approve.assert_not_called()


def test_approve_and_resume_swallows_double_approve_race():
    """If approve() raises (e.g. already-approved race), the helper logs and
    still resumes — a double-approve must not abort the run."""
    conv = _FakeConv(metadata_={
        "_resume_state": {"pending_tool": {"approval_id": "appr-2"}},
    })
    db = MagicMock()
    task = _make_task()
    loop = asyncio.new_event_loop()

    def fake_approve(request_id, reviewed_by, notes=None):
        raise ValueError("Request is already approved")

    with patch("app.services.governance.approval_service.ApprovalService") as ASvc, \
         patch("app.routers.agents.resume_conversation", new=AsyncMock(return_value={"status": "active"})):
        ASvc.return_value.approve = fake_approve
        result = ax._approve_and_resume(loop, db, conv, task)
    loop.close()
    assert result == {"status": "active"}


# -- _run_agent_in_conversation end-to-end (mocked seams) ------------------

def _patch_run_seams(stream_chunks, resume_result_fn=None):
    """Return a context-manager stack patching the seams used by
    _run_agent_in_conversation. ``resume_result_fn(conv)`` returns the dict
    the (patched) _approve_and_resume should yield for each call."""
    patches = []

    db = MagicMock()  # SessionLocal() -> mock; add/commit/refresh are no-ops
    patches.append(patch("app.database.SessionLocal", return_value=db))

    async def fake_stream(**kwargs):
        for c in list(stream_chunks):
            yield c
    patches.append(patch("app.routers.agents.add_message_stream", new=fake_stream))

    patches.append(patch("app.models.agent_conversation.AgentConversation", _FakeConv))

    def resume_stub(loop_, db_, conv, task_):
        if resume_result_fn:
            return resume_result_fn(conv)
        return {"status": "active"}
    patches.append(patch.object(ax, "_approve_and_resume", side_effect=resume_stub))

    return patches, db


def test_skip_conf_true_auto_proceeds_and_completes():
    """skip_confirmation=true: an approval pause is auto-resumed and the run
    reads the final assistant text from the persisted conversation."""
    task = _make_task(skip="true")
    agent = MagicMock(); agent.id = "ag"; agent.name = "Reporter"

    stream = _stream_chunks([{"type": "paused"}])  # approval pause, no reason

    def resume_sets_final(conv):
        conv.messages = [{"role": "assistant", "content": "Report finished."}]
        return {"status": "active"}

    patches, _db = _patch_run_seams(stream, resume_sets_final)
    for p in patches:
        p.start()
    try:
        text, conv_id, _fsm_meta, _tool_outcome = ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
    finally:
        for p in patches:
            p.stop()

    assert text == "Report finished."
    assert conv_id


def test_skip_conf_true_fails_fast_on_decision_summary():
    """Decision-summary pauses are never auto-skipped, even with skip_conf on."""
    task = _make_task(skip="true")
    agent = MagicMock(); agent.id = "ag"; agent.name = "R"

    stream = _stream_chunks([
        {"type": "paused", "reason": "awaiting_decision_summary"},
    ])
    patches, _db = _patch_run_seams(stream)
    for p in patches:
        p.start()
    try:
        try:
            ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
            assert False, "should have raised _AutomationPaused"
        except ax._AutomationPaused as e:
            assert "decision" in str(e).lower()
    finally:
        for p in patches:
            p.stop()


def test_skip_conf_false_fails_on_approval_pause():
    """Without skip_confirmation, an approval pause fails as before."""
    task = _make_task(skip="false")
    agent = MagicMock(); agent.id = "ag"; agent.name = "R"

    stream = _stream_chunks([{"type": "paused"}])
    patches, _db = _patch_run_seams(stream)
    for p in patches:
        p.start()
    try:
        try:
            ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
            assert False, "should have raised _AutomationPaused"
        except ax._AutomationPaused:
            pass  # expected
    finally:
        for p in patches:
            p.stop()


def test_skip_conf_true_bounds_auto_approvals():
    """A run that re-pauses for approval more than MAX_AUTO_APPROVALS times
    fails instead of looping forever."""
    task = _make_task(skip="true")
    agent = MagicMock(); agent.id = "ag"; agent.name = "R"

    stream = _stream_chunks([{"type": "paused"}])  # initial pause

    def always_paused(conv):
        return {"status": "awaiting_approval"}  # never completes

    patches, _db = _patch_run_seams(stream, always_paused)
    for p in patches:
        p.start()
    try:
        try:
            ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
            assert False, "should have raised _AutomationPaused"
        except ax._AutomationPaused as e:
            assert "max" in str(e).lower() or "too many" in str(e).lower() or "cap" in str(e).lower()
        # And it must not have looped unboundedly.
        assert ax._approve_and_resume.call_count <= ax.MAX_AUTO_APPROVALS + 1
    finally:
        for p in patches:
            p.stop()


def test_skip_conf_true_fails_fast_on_decision_summary_during_resume():
    """A decision-summary pause hit DURING a resume (resume_conversation
    returns status="active" with an awaiting_decision_summary tool_call) is
    detected and fails fast — never silently treated as completed."""
    task = _make_task(skip="true")
    agent = MagicMock(); agent.id = "ag"; agent.name = "R"

    stream = _stream_chunks([{"type": "paused"}])  # initial approval pause

    def resume_hits_decision_summary(conv):
        # resume returns status="active" (not awaiting_approval) but stamps
        # an awaiting_decision_summary tool_call on the last assistant message
        # — the marker the executor must detect.
        conv.messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc1", "name": "create_agent",
                            "status": "awaiting_decision_summary"}],
        }]
        return {"status": "active"}

    patches, _db = _patch_run_seams(stream, resume_hits_decision_summary)
    for p in patches:
        p.start()
    try:
        try:
            ax._run_agent_in_conversation(task, agent, "prompt", "exec1")
            assert False, "should have raised _AutomationPaused"
        except ax._AutomationPaused as e:
            assert "decision" in str(e).lower()
    finally:
        for p in patches:
            p.stop()
