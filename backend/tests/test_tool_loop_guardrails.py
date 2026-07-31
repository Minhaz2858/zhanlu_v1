"""Tests for the per-turn tool-call loop guardrail controller.

Covers the 3 loop patterns Hermes detects that Zhanlu's history-scan
_detect_tool_call_loop does not:
  1. same-tool-failure (same tool, DIFFERENT args, all failing)
  2. no-progress (idempotent tool returning identical results)
  3. warn-before-block escalation
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_loop_guardrails import (
    ToolLoopGuardController,
    ToolGuardrailConfig,
    synthetic_blocked_result,
)


def test_same_tool_different_args_failure_loop_trips():
    """Same tool failing with DIFFERENT args 8 times should halt."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    args_sequence = [{"query": f"q{i}"} for i in range(8)]
    decision = None
    for args in args_sequence:
        ctrl.before_call("web_search", args)
        decision = ctrl.after_call("web_search", args, json.dumps({"success": False, "error": "boom"}))
        if decision.should_halt:
            break
    assert ctrl.halt_decision is not None
    assert ctrl.halt_decision.code == "same_tool_failure_halt"


def test_no_progress_idempotent_loop_trips():
    """read_file returning the same content 5 times should block on the 6th call.

    The block fires in before_call (which checks accumulated counts from
    prior after_call observations). So 5 after_calls build repeat_count to
    5, and the 6th before_call trips the no_progress_block threshold.
    """
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    result = json.dumps({"success": True, "content": "same file body"})
    for _ in range(5):
        ctrl.before_call("read_file", {"path": "/a.txt"})
        ctrl.after_call("read_file", {"path": "/a.txt"}, result)
    # 5 after_calls -> repeat_count = 5. The 6th before_call should block.
    decision = ctrl.before_call("read_file", {"path": "/a.txt"})
    assert decision.should_halt
    assert ctrl.halt_decision is not None
    assert ctrl.halt_decision.code == "no_progress_block"


def test_warn_before_block_when_warnings_enabled():
    """With warnings on + hard_stop off, repeated exact failures warn but don't block."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(warnings_enabled=True, hard_stop_enabled=False))
    args = {"path": "/missing.txt"}
    decision = None
    for _ in range(3):
        ctrl.before_call("read_file", args)
        decision = ctrl.after_call("read_file", args, json.dumps({"success": False, "error": "nope"}))
    # Should have warned (action=warn), not halted
    assert not ctrl.halt_decision
    assert decision is not None
    assert decision.action == "warn"


def test_success_resets_failure_counter():
    """A success after failures resets the exact-failure counter."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    args = {"path": "/x.txt"}
    # 4 failures (under halt threshold of 8)
    for _ in range(4):
        ctrl.before_call("read_file", args)
        ctrl.after_call("read_file", args, json.dumps({"success": False}))
    # One success
    ctrl.before_call("read_file", args)
    ctrl.after_call("read_file", args, json.dumps({"success": True}))
    # 4 more failures -- should NOT trip because counter was reset
    for _ in range(4):
        ctrl.before_call("read_file", args)
        ctrl.after_call("read_file", args, json.dumps({"success": False}))
    assert ctrl.halt_decision is None


def test_different_results_do_not_trip_no_progress():
    """read_file returning DIFFERENT content each time is NOT a no-progress loop."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    for i in range(6):
        result = json.dumps({"success": True, "content": f"version {i}"})
        ctrl.before_call("read_file", {"path": "/a.txt"})
        ctrl.after_call("read_file", {"path": "/a.txt"}, result)
    assert ctrl.halt_decision is None


def test_mutation_tool_never_trips_no_progress():
    """write_file is not idempotent; repeated identical calls are not no-progress."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    result = json.dumps({"success": True})
    for _ in range(6):
        ctrl.before_call("write_file", {"path": "/a.txt", "content": "x"})
        ctrl.after_call("write_file", {"path": "/a.txt", "content": "x"}, result)
    assert ctrl.halt_decision is None


def test_synthetic_blocked_result_is_valid_json():
    """The synthetic result for a blocked call must be valid JSON with success=False."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    args = {"path": "/x.txt"}
    for _ in range(6):
        ctrl.before_call("read_file", args)
        ctrl.after_call("read_file", args, json.dumps({"success": False, "error": "err"}))
    assert ctrl.halt_decision is not None
    synthetic = synthetic_blocked_result(ctrl.halt_decision)
    parsed = json.loads(synthetic)
    assert parsed["success"] is False
    assert "error" in parsed
    assert "guardrail" in parsed


def test_reset_for_turn_clears_state():
    """reset_for_turn clears all counters and halt state."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    # Trip a halt
    for _ in range(8):
        ctrl.before_call("read_file", {"path": "/x.txt"})
        ctrl.after_call("read_file", {"path": "/x.txt"}, json.dumps({"success": False}))
    assert ctrl.halt_decision is not None
    # Reset
    ctrl.reset_for_turn()
    assert ctrl.halt_decision is None
    # Can trip again after reset
    for _ in range(8):
        ctrl.before_call("read_file", {"path": "/x.txt"})
        ctrl.after_call("read_file", {"path": "/x.txt"}, json.dumps({"success": False}))
    assert ctrl.halt_decision is not None
