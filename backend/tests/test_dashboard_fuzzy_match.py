"""Fix 5 tests: flag-gated fuzzy 'dashboard' matching + flag-aware rescue tool.

Covers the four detector call sites (``dashboard_turn_guard.is_live_dashboard_request``,
``turn_action``, ``agents._is_dashboard_request``, ``intent_router.detect_file_intent``)
and the v3 dashboard rescue (which must force the flag-aware build tool name,
never the hardcoded legacy ``create_dashboard``).
"""
import re
from pathlib import Path

import pytest

from app.services.dashboard_turn_guard import (
    _fuzzy_dashboard_word,
    dashboard_build_tool,
    fuzzy_dashboard_request,
    is_live_dashboard_request,
)
from app.services.synexia.intent_router import detect_file_intent

_BACKEND = Path(__file__).resolve().parents[1]
_AGENTS_SRC = (_BACKEND / "app/routers/agents.py").read_text(encoding="utf-8")
_TURN_ACTION_SRC = (_BACKEND / "app/services/turn_action.py").read_text(encoding="utf-8")
_INTENT_ROUTER_SRC = (_BACKEND / "app/services/synexia/intent_router.py").read_text(encoding="utf-8")


def _flag_on(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "DASHBOARD_FUZZY_MATCH_ENABLED", True)


def _flag_off(monkeypatch) -> None:
    from app.config import settings
    monkeypatch.setattr(settings, "DASHBOARD_FUZZY_MATCH_ENABLED", False)


# ── _fuzzy_dashboard_word ──────────────────────────────────────────────────
@pytest.mark.parametrize("token", [
    "dashbord", "Dashbord", "dashboard", "dash-board", "dashboards",
    "dashbaord", "DASHBOARD", "dashbords",
])
def test_fuzzy_word_matches_typos_when_enabled(monkeypatch, token):
    _flag_on(monkeypatch)
    assert _fuzzy_dashboard_word(token) is True


@pytest.mark.parametrize("token", [
    "dash", "db", "data", "board", "dashb", "dashbordx", "dahsboard",
])
def test_fuzzy_word_rejects_short_and_unrelated(monkeypatch, token):
    _flag_on(monkeypatch)
    assert _fuzzy_dashboard_word(token) is False


def test_fuzzy_word_off_when_flag_disabled(monkeypatch):
    _flag_off(monkeypatch)
    assert _fuzzy_dashboard_word("dashbord") is False


# ── fuzzy_dashboard_request ────────────────────────────────────────────────
def test_fuzzy_request_flag_gated(monkeypatch):
    _flag_off(monkeypatch)
    assert fuzzy_dashboard_request("make me a Dashbord") is False
    _flag_on(monkeypatch)
    assert fuzzy_dashboard_request("make me a Dashbord") is True
    assert fuzzy_dashboard_request("build a sales dash-board now") is True


# ── is_live_dashboard_request ──────────────────────────────────────────────
def test_is_live_dashboard_request_fuzzy_typo(monkeypatch):
    _flag_on(monkeypatch)
    assert is_live_dashboard_request("make me a Dashbord for sales") is True


def test_is_live_dashboard_request_fuzzy_off(monkeypatch):
    _flag_off(monkeypatch)
    assert is_live_dashboard_request("make me a Dashbord for sales") is False


def test_is_live_dashboard_request_zh_intact(monkeypatch):
    _flag_on(monkeypatch)
    assert is_live_dashboard_request("帮我做一个仪表盘") is True
    assert is_live_dashboard_request("帮我做报表") is False


# ── detect_file_intent (intent_router dashboard branch) ────────────────────
def test_detect_file_intent_fuzzy_dashboard_when_enabled(monkeypatch):
    _flag_on(monkeypatch)
    assert detect_file_intent("can you build a Dashbord of sales?") == "dashboard"


def test_detect_file_intent_fuzzy_dashboard_off(monkeypatch):
    _flag_off(monkeypatch)
    assert detect_file_intent("can you build a Dashbord of sales?") is None


def test_detect_file_intent_exact_dashboard_still_matches(monkeypatch):
    _flag_off(monkeypatch)
    assert detect_file_intent("build a dashboard for me") == "dashboard"


# ── detector call-site wiring (source checks) ─────────────────────────────
def test_turn_action_uses_fuzzy_helper_at_both_sites():
    lines = _TURN_ACTION_SRC.splitlines()
    idxs = [i for i, ln in enumerate(lines) if "LIVE_DASHBOARD_PATTERN.search" in ln]
    assert len(idxs) >= 2, "expected both LIVE_DASHBOARD_PATTERN sites in turn_action.py"
    for i in idxs:
        window = "\n".join(lines[i : i + 4])
        assert "fuzzy_dashboard_request" in window, (
            f"site not fuzzy-wired near line {i + 1}"
        )


def test_agents_is_dashboard_request_uses_fuzzy_helper():
    m = re.search(
        r"def _is_dashboard_request\(.*?(?=\n\ndef |\n\Z)", _AGENTS_SRC, re.S
    )
    assert m, "def _is_dashboard_request not found in agents.py"
    assert "fuzzy_dashboard_request" in m.group(0)


def test_intent_router_dashboard_branch_uses_fuzzy_helper():
    assert "fuzzy_dashboard_request" in _INTENT_ROUTER_SRC
    assert 'return "dashboard"' in _INTENT_ROUTER_SRC


def test_rescue_uses_flag_aware_build_tool():
    assert "Dashboard rescue: user asked for dashboard" in _AGENTS_SRC, (
        "v3 dashboard rescue log not found"
    )
    # Anchor on the computed-tool assignment so the window covers the guard
    # condition, the rescue nudge, the forced tool_choice, and the execution
    # filter.
    m = re.search(r"_rescue_build_tool = dashboard_build_tool\(\)", _AGENTS_SRC)
    assert m, "rescue build-tool computation not found"
    tail = _AGENTS_SRC[m.start() : m.start() + 3200]
    # The rescue must compute the active build tool (never hardcode legacy).
    assert "_rescue_build_tool" in tail
    # The forced tool_choice name must be the computed tool, not a literal.
    tc = re.search(
        r'tool_choice=\{"type": "function", "function": \{"name": ([^}]+)\}\}',
        tail,
    )
    assert tc, "rescue tool_choice not found"
    assert "_rescue_build_tool" in tc.group(1), (
        f"rescue forces a hardcoded tool name: {tc.group(1)}"
    )
    # The tool-was-called guard must check the computed tool too.
    assert f'_tool_was_called(tool_calls_for_frontend, _rescue_build_tool)' in tail


def test_dashboard_build_tool_returns_active_tool(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    assert dashboard_build_tool() == "create_fullstack_dashboard"
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    assert dashboard_build_tool() == "create_dashboard"
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    assert dashboard_build_tool() is None
