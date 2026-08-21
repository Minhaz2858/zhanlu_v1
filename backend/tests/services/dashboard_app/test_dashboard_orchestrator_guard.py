"""Fix A + Fix C: post-loop orchestrator routing regression tests.

BUG A (P0 crash) — ``_orch_created`` NameError: agents.py's finalize phase
referenced ``_orch_created`` inside ``_choose_fallback(...)`` (the no-content
fallback path) BEFORE it was assigned — the assignment lived at the top of the
orchestrator ``try`` block that runs AFTER the fallback. Every turn that hit
the fallback path with no assistant_content crashed the SSE generator
(``cannot access local variable '_orch_created'``) → 0ms "Generating response"
+ fallback done.

BUG C (routing conflict) — the finalize-phase orchestrator
(``ensure_artifact_for_doc_request(doc_format="dashboard")``) runs AFTER the
v3 loop breaks, OUTSIDE the ``DASHBOARD_ANTITOOLS`` interception block. On a
dashboard-intent turn where the build tool was never called, it auto-created a
static HTML "Dashboard" artifact (``orch-`` prefix) — silently shipping a
static report card instead of the requested dashboard (conversation caeeda3b).

Fixes:
- A: init ``_orch_created: list[dict] = []`` before the fallback reference.
- C: ``dashboard_orchestrator_should_block`` predicate (dashboard-intent +
  build tool enabled + build tool NOT called) gates the ENTIRE orchestrator
  try block; when it fires, agents.py skips the block and appends a synthetic
  BLOCKED record to ``tool_calls_for_frontend``.
"""
import ast
import importlib
import inspect
from pathlib import Path

import pytest

from app.config import settings
from app.services.dashboard_turn_guard import (
    dashboard_build_tool,
    dashboard_orchestrator_should_block,
)

AGENTS_PY = Path(
    inspect.getsourcefile(importlib.import_module("app.routers.agents"))
)


@pytest.fixture
def fullstack_on(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    yield


# ── Fix C predicate: dashboard_orchestrator_should_block ────────────────────

def test_orch_guard_fires_pre_build(fullstack_on):
    """Dashboard turn, nothing executed yet → the orchestrator must be
    skipped so it cannot fabricate a static Dashboard artifact."""
    assert dashboard_orchestrator_should_block("build me a dashboard", []) is True
    assert dashboard_orchestrator_should_block("make an ERP Dashboard", None) is True


def test_orch_guard_fires_after_exploration_only(fullstack_on):
    """Query-only executed names do NOT satisfy the build requirement — the
    loop-guard break on caeeda3b (ask_data_agent ×2) must still block the
    orchestrator."""
    executed = ["list_data_sources", "ask_data_agent", "ask_data_agent"]
    assert dashboard_orchestrator_should_block("build me a dashboard", executed) is True


def test_orch_guard_off_after_build(fullstack_on):
    """After create_fullstack_dashboard ran, the orchestrator may run (static
    export / marker fulfillment is legitimate)."""
    executed = ["create_fullstack_dashboard", "describe_schema"]
    assert dashboard_orchestrator_should_block("build me a dashboard", executed) is False


def test_orch_guard_inert_for_non_dashboard(fullstack_on):
    assert dashboard_orchestrator_should_block("summarize the report", []) is False
    assert dashboard_orchestrator_should_block("", []) is False
    assert dashboard_orchestrator_should_block(None, []) is False


def test_orch_guard_inert_when_flags_off(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    assert dashboard_orchestrator_should_block("build me a dashboard", []) is False


def test_orch_guard_active_in_legacy_mode(monkeypatch):
    """Legacy mode: the guard is active (build tool = create_dashboard), but
    the orchestrator must still be blocked pre-build."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    assert dashboard_build_tool() == "create_dashboard"
    assert dashboard_orchestrator_should_block("build me a dashboard", []) is True


def test_orch_guard_legacy_off_after_build(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    executed = ["create_dashboard"]
    assert dashboard_orchestrator_should_block("build me a dashboard", executed) is False


# ── Fix A structural regression: _orch_created init ordering ────────────────

def _v3_stream_func_ast():
    """AST function node of the v3 streaming generator (contains both the
    fallback reference and the orchestrator import)."""
    source = AGENTS_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            seg = ast.get_source_segment(source, node)
            if seg and "_choose_fallback(" in seg and "generation_orchestrator" in seg:
                return node
    raise AssertionError("v3 stream function with orchestrator not found in agents.py")


def test_orch_created_initialized_before_fallback_reference():
    """BUG A regression: `_orch_created` must be assigned BEFORE the
    `_choose_fallback(...)` call that references it. Previously the init lived
    inside the orchestrator try block (after the fallback), so the no-content
    path crashed with `cannot access local variable '_orch_created'`."""
    fn = _v3_stream_func_ast()
    init_lines: list[int] = []
    fallback_line: int | None = None
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "_orch_created" for t in targets):
                init_lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_choose_fallback"
        ):
            fallback_line = node.lineno
    assert init_lines, "no `_orch_created` assignment found in v3 stream"
    assert fallback_line is not None, "no `_choose_fallback` call found in v3 stream"
    assert min(init_lines) < fallback_line, (
        f"_orch_created first assigned at line {min(init_lines)} AFTER the "
        f"fallback reference at line {fallback_line} — BUG A regressed"
    )


# ── Fix C structural regression: orchestrator guard wiring ──────────────────

def test_orch_guard_wired_before_orchestrator_try():
    """BUG C regression: the finalize phase must evaluate
    `dashboard_orchestrator_should_block` and raise the skip sentinel BEFORE
    the orchestrator import, and catch it in a dedicated except clause — so a
    dashboard turn that never called the build tool can't auto-create a static
    Dashboard artifact."""
    src = AGENTS_PY.read_text(encoding="utf-8")
    seg = ast.get_source_segment(src, _v3_stream_func_ast())
    guard_idx = seg.index("_orch_guard_should_block = dashboard_orchestrator_should_block(")
    orch_import_idx = seg.index("from app.services.generation_orchestrator import")
    assert guard_idx < orch_import_idx, (
        "orchestrator guard evaluated AFTER the orchestrator import — "
        "BUG C routing conflict regressed"
    )
    assert "raise _OrchGuardSkipped()" in seg, "skip sentinel not raised in finalize phase"
    assert "except _OrchGuardSkipped" in seg, "skip sentinel not caught in finalize phase"


def test_orch_guard_symbol_imported_by_agents():
    """Import smoke: agents.py must import the new guard symbol (mirrors the
    tuple test in test_dashboard_narration_nudge.py)."""
    agents_mod = importlib.import_module("app.routers.agents")
    assert hasattr(agents_mod, "dashboard_orchestrator_should_block")
