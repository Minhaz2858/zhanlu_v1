"""T19 orchestrator-guard regression: a FAILED build tool must still block
the post-loop orchestrator.

Smoke #5 (2026-08-21): ``create_fullstack_dashboard`` crashed with
``NameError: name 'Path' is not defined``, yet its name remained in
``_v3_executed_tool_names`` — so ``dashboard_orchestrator_should_block``
returned False ("build tool ran → orchestrator allowed") and the orchestrator
fabricated a 78 KB static "Web page" artifact on top of the crashed build.

Fix: agents.py tracks ``_v3_failed_tool_names`` (tools whose result was
``{"success": False}`` or that threw — exceptions are converted to that shape
in ``_run_tool_batch``); the guard receives that set and returns True (block
the orchestrator) when the build tool is in the failed set.
"""
import pytest

from app.config import settings
from app.services.dashboard_turn_guard import (
    dashboard_build_tool,
    dashboard_orchestrator_should_block,
)

BUILD = "create_fullstack_dashboard"


@pytest.fixture
def fullstack_on(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    yield


def test_orch_guard_blocks_when_build_tool_failed(fullstack_on):
    """Build tool was called but failed → orchestrator must be blocked (no
    static-artifact fabrication on top of a crashed build)."""
    assert dashboard_build_tool() == BUILD
    assert dashboard_orchestrator_should_block(
        "build me a dashboard", [BUILD], {BUILD},
    ) is True


def test_orch_guard_allows_when_build_tool_succeeded(fullstack_on):
    """Same executed names but no failure recorded → orchestrator allowed.
    Default ``failed_names=None`` (existing callers) stays backward-compatible."""
    assert dashboard_orchestrator_should_block(
        "build me a dashboard", [BUILD],
    ) is False
    assert dashboard_orchestrator_should_block(
        "build me a dashboard", [BUILD], set(),
    ) is False


def test_orch_guard_failed_names_ignored_for_non_dashboard(fullstack_on):
    """failed_names only matters on dashboard-intent turns — non-dashboard
    requests stay inert regardless of failures."""
    assert dashboard_orchestrator_should_block(
        "summarize the report", [BUILD], {BUILD},
    ) is False
    assert dashboard_orchestrator_should_block(None, [BUILD], {BUILD}) is False


def test_orch_guard_failed_names_inert_when_flags_off(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    assert dashboard_orchestrator_should_block(
        "build me a dashboard", [BUILD], {BUILD},
    ) is False


def test_orch_guard_legacy_failed_build_blocks(monkeypatch):
    """Legacy mode: a failed ``create_dashboard`` call must also block the
    orchestrator; a successful one still allows it."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    assert dashboard_build_tool() == "create_dashboard"
    assert dashboard_orchestrator_should_block(
        "build me a dashboard", ["create_dashboard"], {"create_dashboard"},
    ) is True
    assert dashboard_orchestrator_should_block(
        "build me a dashboard", ["create_dashboard"],
    ) is False


def test_orch_guard_other_failed_tools_do_not_override(fullstack_on):
    """A failed NON-build tool must NOT unlock the orchestrator — the build
    tool itself was never called, so it still blocks."""
    assert dashboard_orchestrator_should_block(
        "build me a dashboard", ["execute_query"], {"execute_query"},
    ) is True
