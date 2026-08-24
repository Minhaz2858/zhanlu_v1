"""Unit tests for the extracted batch-level guardrail helpers (P2-12).

Covers ``app.services.agent_loop.guardrails``:
- ``maybe_force_finish_line``: finish-line ``tool_choice="none"`` override
  with dashboard-forcing precedence.
- ``apply_guardrails``: partition a batch into executable/blocked.
- ``enforce_tool_caps``: per-tool cap resolution (dynamic > static > hard).
- ``maybe_wrap_up_nudge``: T-minus-N nudge policy (once per turn).
- ``pause_for_approval``: awaiting-approval record + pending-tool payload.
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.agent_loop.guardrails import (
    apply_guardrails,
    enforce_tool_caps,
    maybe_force_finish_line,
    maybe_wrap_up_nudge,
    pause_for_approval,
)


class _Decision:
    def __init__(self, allows: bool):
        self.allows_execution = allows


# ---------------------------------------------------------------------------
# maybe_force_finish_line
# ---------------------------------------------------------------------------

def test_finish_line_forces_none_on_final_iteration():
    # ``final_iteration`` is the last value the loop variable takes; forcing
    # happens when ``iteration >= final_iteration``.
    assert maybe_force_finish_line(10, 10, False, None) == "none"
    assert maybe_force_finish_line(10, 10, False, {"function": {"name": "query"}}) == "none"


def test_finish_line_keeps_tool_choice_before_final():
    choice = {"function": {"name": "query"}}
    assert maybe_force_finish_line(9, 10, False, choice) is choice
    assert maybe_force_finish_line(9, 10, False, None) is None


def test_finish_line_dashboard_forced_wins():
    choice = {"function": {"name": "create_dashboard"}}
    assert maybe_force_finish_line(10, 10, True, choice) is choice


# ---------------------------------------------------------------------------
# apply_guardrails
# ---------------------------------------------------------------------------

def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"tool_name": name, "args": args, "tool_call_id": call_id, "args_str": json.dumps(args)}


def test_apply_guardrails_partitions_executable_and_blocked():
    calls = [_call("ok", {}), _call("no", {})]

    def before_call(name, args):
        return _Decision(name == "ok")

    executable, blocked = apply_guardrails(calls, before_call=before_call)
    assert [c["tool_name"] for c in executable] == ["ok"]
    assert blocked == [{"success": False, "error": "blocked by guardrail"}]


def test_apply_guardrails_uses_blocked_result_factory():
    calls = [_call("no", {})]

    def before_call(name, args):
        return _Decision(False)

    def blocked_result_factory(gd):
        return json.dumps({"success": False, "error": "synthetic", "guardrail": True})

    executable, blocked = apply_guardrails(
        calls, before_call=before_call, blocked_result_factory=blocked_result_factory
    )
    assert executable == []
    assert blocked == [{"success": False, "error": "synthetic", "guardrail": True}]


# ---------------------------------------------------------------------------
# enforce_tool_caps
# ---------------------------------------------------------------------------

def test_enforce_tool_caps_dynamic_over_static_over_hard():
    caps = {"query": 2}
    dynamic = {"query": 1}
    assert enforce_tool_caps("query", 1, caps=caps, dynamic_caps=dynamic) is True
    assert enforce_tool_caps("query", 0, caps=caps, dynamic_caps=dynamic) is False
    # Static cap applies when no dynamic entry.
    assert enforce_tool_caps("query", 2, caps=caps) is True
    assert enforce_tool_caps("query", 1, caps=caps) is False
    # Hard cap as the floor.
    assert enforce_tool_caps("other", 5, hard_cap=5) is True


def test_enforce_tool_caps_failures_burn_budget():
    # Blocked when executed >= cap, or failures >= cap + 1.
    assert enforce_tool_caps("query", 2, caps={"query": 2}) is True
    assert enforce_tool_caps("query", 0, failed_count=3, caps={"query": 2}) is True
    assert enforce_tool_caps("query", 0, failed_count=2, caps={"query": 2}) is False


def test_enforce_tool_caps_no_cap_never_blocks():
    assert enforce_tool_caps("query", 999) is False


# ---------------------------------------------------------------------------
# maybe_wrap_up_nudge
# ---------------------------------------------------------------------------

def test_wrap_up_nudge_within_margin_once():
    assert maybe_wrap_up_nudge(7, 10, margin=3) is not None
    assert maybe_wrap_up_nudge(6, 10, margin=3) is None
    # Already nudged this turn -> never nudges again.
    assert maybe_wrap_up_nudge(7, 10, margin=3, already_nudged=True) is None


# ---------------------------------------------------------------------------
# pause_for_approval
# ---------------------------------------------------------------------------

def test_pause_for_approval_builds_record_and_pending():
    call = {
        "tool_name": "write_to_erp",
        "args": {"order": 1},
        "args_str": '{"order": 1}',
        "tool_call_id": "tc-1",
        "approval_id": "ap-1",
    }
    result = {"requires_approval": True, "approval_id": "ap-1", "reason": "writes ERP"}
    record, pending = pause_for_approval(result, call, "Write to ERP", remaining_calls=[call])
    assert record["id"] == "tc-1"
    assert record["name"] == "Write to ERP"
    assert record["status"] == "awaiting_approval"
    assert record["approval_id"] == "ap-1"
    assert pending["tool_name"] == "write_to_erp"
    assert pending["approval_id"] == "ap-1"
    assert pending["remaining_calls"] == [call]


def test_pause_for_approval_noop_without_flag():
    call = {"tool_name": "query", "args": {}, "args_str": "{}", "tool_call_id": "tc-1"}
    record, pending = pause_for_approval({"success": True}, call, "Query")
    assert record is None and pending is None
