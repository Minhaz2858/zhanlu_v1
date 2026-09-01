"""Finish-line forced-answer + interrupt waste-removal tests.

Covers the agent tool-budget reliability work:

1. ``_finish_line_tool_choice`` forces ``tool_choice="none"`` on the final
   iteration so the LLM must synthesize an answer in text; the dashboard
   guard's ``create_dashboard`` forcing takes precedence.
2. Per-tool caps ``TOOL_CALL_CAPS`` include ``interrupt: 2`` and
   ``clarify: 3``, and ``_detect_tool_call_loop`` enforces them.
3. ``interrupt`` is removed from default LLM exposure (``enabled_by_default``
   off, absent from system-agent ``ALL_TOOL_NAMES``, classified no-effect)
   but remains registered so an explicit ``tool_config`` re-enable still works.
4. Budgets raised: ``MAX_TOOL_ITERATIONS == 40``, ``AGENT_MAX_ITERATIONS == 100``.
5. The T-3 wrap-up nudge + finish-line forcing are wired into the v3 loop.
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _tc(name: str, args: dict, call_id: str = None) -> dict:
    """Build a tool_call entry in OpenAI format."""
    cid = call_id or f"call_{name}_{hash(json.dumps(args, sort_keys=True))}"
    return {
        "id": cid,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, sort_keys=True),
        },
    }


def _msg(role: str, content: str = "", tool_calls=None, tool_call_id: str = None) -> dict:
    out = {"role": role, "content": content}
    if tool_calls:
        out["tool_calls"] = tool_calls
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    return out


_CREATE_DASHBOARD = {
    "type": "function",
    "function": {"name": "create_dashboard"},
}


class TestFinishLineToolChoice:
    """Pure-function tests for ``_finish_line_tool_choice``."""

    def test_final_iteration_forces_none(self):
        from app.routers.agents import _finish_line_tool_choice
        assert _finish_line_tool_choice(39, 39, False, None) == "none"

    def test_final_iteration_forces_none_over_existing_tool_choice(self):
        from app.routers.agents import _finish_line_tool_choice
        assert _finish_line_tool_choice(39, 39, False, _CREATE_DASHBOARD) == "none"

    def test_non_final_iteration_preserves_tool_choice(self):
        from app.routers.agents import _finish_line_tool_choice
        assert _finish_line_tool_choice(20, 39, False, _CREATE_DASHBOARD) == _CREATE_DASHBOARD

    def test_non_final_iteration_none_stays_none(self):
        from app.routers.agents import _finish_line_tool_choice
        assert _finish_line_tool_choice(20, 39, False, None) is None

    def test_dashboard_forced_wins_on_final_iteration(self):
        """Calling create_dashboard IS the finish line — it wins over "none"."""
        from app.routers.agents import _finish_line_tool_choice
        assert _finish_line_tool_choice(39, 39, True, _CREATE_DASHBOARD) == _CREATE_DASHBOARD

    def test_dashboard_forced_on_non_final_iteration_preserved(self):
        from app.routers.agents import _finish_line_tool_choice
        assert _finish_line_tool_choice(38, 39, True, _CREATE_DASHBOARD) == _CREATE_DASHBOARD


class TestInterruptCaps:
    """Per-tool caps and the loop guard enforcement."""

    def test_tool_call_caps_include_interrupt_and_clarify(self):
        from app.routers.agents import TOOL_CALL_CAPS
        assert TOOL_CALL_CAPS["interrupt"] == 2
        assert TOOL_CALL_CAPS["clarify"] == 3

    def test_two_successful_interrupt_checks_trip(self):
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "are you still working?"),
            _msg("assistant", "", tool_calls=[_tc("interrupt", {"action": "check"}, "c1")]),
            _msg("tool", json.dumps({"success": True}), tool_call_id="c1"),
            _msg("assistant", "", tool_calls=[_tc("interrupt", {"action": "check"}, "c2")]),
            _msg("tool", json.dumps({"success": True}), tool_call_id="c2"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is not None, "2 successful interrupt checks should trip cap=2"
        assert info[0] == "interrupt"

    def test_one_interrupt_check_does_not_trip(self):
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "are you still working?"),
            _msg("assistant", "", tool_calls=[_tc("interrupt", {"action": "check"}, "c1")]),
            _msg("tool", json.dumps({"success": True}), tool_call_id="c1"),
        ]
        assert _detect_tool_call_loop(messages) is None


class TestInterruptDefaultExposure:
    """``interrupt`` is off the default LLM tool list but stays registered."""

    def test_interrupt_disabled_by_default_but_registered(self):
        from app.services.tool_registry import registry
        entry = registry.get_entry("interrupt")
        assert entry is not None, "interrupt must stay registered (explicit re-enable)"
        assert entry.enabled_by_default is False

    def test_interrupt_absent_from_system_agent_tool_list(self):
        from app.services.system_agents import ALL_TOOL_NAMES
        assert "interrupt" not in ALL_TOOL_NAMES

    def test_interrupt_in_no_effect_tool_names(self):
        from app.services.tool_result_classification import NO_EFFECT_TOOL_NAMES
        assert "interrupt" in NO_EFFECT_TOOL_NAMES


class TestBudgets:
    """Raised per-turn and per-conversation budgets."""

    def test_max_tool_iterations_is_40(self):
        from app.routers.agents import MAX_TOOL_ITERATIONS
        assert MAX_TOOL_ITERATIONS == 40

    def test_agent_max_iterations_is_100(self):
        from app.config import settings
        assert settings.AGENT_MAX_ITERATIONS == 100


class TestLoopWiring:
    """The finish-line helper + wrap-up nudge are wired into the loop source."""

    def test_v3_loop_wires_finish_line(self):
        src_path = os.path.join(_BACKEND_ROOT, "app", "routers", "agents.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "_finish_line_tool_choice(" in src
        assert "You have 3 steps left. Stop exploring and produce " in src
        assert "_wrapup_nudged" in src
        # The nudge fires exactly once at T-3 (guard flag + set flag).
        assert "iteration == MAX_TOOL_ITERATIONS - 3 and not _wrapup_nudged" in src
        assert "_wrapup_nudged = True" in src
