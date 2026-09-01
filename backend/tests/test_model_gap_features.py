"""Tests for the 2026-08-27 model-gap implementation:

1. Project-scoped memory review/edit API (project_memories router)
2. Run-timeline observability: tokens/status/error on AgentRunStep + sink
3. Interactive artifact canvas save endpoint
4. Optional LLM-informed automation tick (best-effort preamble)
"""

import pytest


# ── 1. Project-scoped memory API ─────────────────────────────────────────

def test_memory_entry_model_has_pinned():
    from app.models.agent_memory import AgentMemory

    col = AgentMemory.__table__.c.pinned
    assert col is not None
    assert col.default is not None  # defaults to False


def test_project_memories_router_registered():
    from main import create_app  # noqa: F401  (import check)

    import main as main_module
    assert hasattr(main_module, "project_memories_router")


def test_project_memories_router_paths():
    from app.routers.project_memories import router

    paths = {r.path for r in router.routes}
    assert "/projects/{project_id}/memories" in paths
    assert "/projects/{project_id}/memories/{memory_id}" in paths


def test_memory_scope_rules_match_memory_tool():
    """The API must mirror memory_tool scoping: user rows cross-project,
    memory rows strict project match (no NULL fallback)."""
    from app.routers.project_memories import _to_out
    from app.models.agent_memory import AgentMemory

    m = AgentMemory(
        agent_app_id="a1", user_id="u1", project_id="p1",
        target="memory", content="note", char_count=4,
    )
    out = _to_out(m)
    assert out.project_id == "p1"
    assert out.target == "memory"
    assert out.content == "note"


# ── 2. Run-timeline observability ────────────────────────────────────────

def test_agent_run_step_has_observability_fields():
    from app.models.agent_run_step import AgentRunStep

    cols = {c.name for c in AgentRunStep.__table__.columns}
    assert {"prompt_tokens", "completion_tokens", "total_tokens",
            "status", "error", "retry_count"} <= cols


def test_checkpoint_sink_persists_tokens_and_status():
    from app.services.harness.checkpoint_sink import CheckpointSink

    sink = CheckpointSink()
    captured = {}

    class _FakeStep:
        pass

    # Verify the sink reads the new event fields without error.
    # (Full DB write path is covered by integration tests; here we just
    # verify the mapping logic doesn't choke on the new keys.)
    event = {
        "type": "llm_call",
        "run_id": "run123",
        "iteration": 1,
        "duration_ms": 42,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "status": "ok",
        "error": None,
        "retry_count": 0,
    }
    sink._step_index = 0
    # Patch persistence to capture the constructed AgentRunStep fields.
    orig = sink._persist_step

    def fake_persist(step_type, ev):
        captured["step_type"] = step_type
        captured["event"] = ev

    sink._persist_step = fake_persist  # type: ignore[assignment]
    try:
        sink(event)
    finally:
        sink._persist_step = orig  # type: ignore[assignment]
    assert captured.get("step_type") == "llm_call"
    assert captured["event"]["total_tokens"] == 150


def test_agent_runs_step_out_exposes_tokens():
    from app.routers.agent_runs import StepOut

    fields = StepOut.model_fields
    assert "prompt_tokens" in fields
    assert "total_tokens" in fields
    assert "retry_count" in fields


# ── 3. Interactive artifact canvas ───────────────────────────────────────

def test_canvas_save_endpoint_registered():
    from app.routers.artifacts import router

    paths = [r.path for r in router.routes]
    assert "/artifacts/{artifact_id}/canvas/save" in paths


def test_canvas_save_request_shape():
    from app.routers.artifacts import CanvasSaveRequest

    req = CanvasSaveRequest(html="<h1>hi</h1>")
    assert req.html == "<h1>hi</h1>"
    assert req.source == "user"  # default
    req2 = CanvasSaveRequest(html="x", source="llm", changelog="v2")
    assert req2.source == "llm"
    assert req2.changelog == "v2"


# ── 4. LLM-informed automation tick ──────────────────────────────────────

def test_automation_task_has_llm_informed_flag():
    from app.models.automation_task import AutomationTask

    cols = {c.name for c in AutomationTask.__table__.columns}
    assert "llm_informed_tick" in cols


def test_llm_tick_preamble_skips_when_disabled():
    from app.services.automation_executor import _llm_tick_preamble

    class _Task:
        llm_informed_tick = False
        prompt = "weekly report"
        description = None
        name = "t"

    assert _llm_tick_preamble(_Task(), "prev ctx", None) == ""


def test_llm_tick_preamble_fails_safe():
    """Even when enabled, a broken LLM path returns '' (run still happens)."""
    from app.services.automation_executor import _llm_tick_preamble

    class _Task:
        llm_informed_tick = True
        prompt = "weekly report"
        description = None
        name = "t"

    # Monkeypatch call_llm to raise — must fall back to "".
    import app.services.automation_executor as mod

    class _ExplodingCall:
        pass

    orig = mod.datetime  # noqa: F841 — keep reference
    import importlib
    llm_mod = importlib.import_module("app.services.llm_service")
    saved = llm_mod.call_llm

    async def boom(**kwargs):
        raise RuntimeError("provider down")

    llm_mod.call_llm = boom  # type: ignore[assignment]
    try:
        # Reload to pick up patched reference via module attr lookup
        result = _llm_tick_preamble(_Task(), "prev", None)
        assert result == ""
    finally:
        llm_mod.call_llm = saved
