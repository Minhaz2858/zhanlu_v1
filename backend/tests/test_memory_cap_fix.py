"""Regression tests for the empty-bubble collapse fix (2026-10-09).

Bug: TOOL_CALL_CAPS["memory"] = 1 + the guard tripping at ``succ >= cap``
meant the FIRST successful memory call armed the loop guard; the granted
"tool-free synthesis pass" still exposed tools, weak models re-called
memory, the loop broke empty and the turn collapsed to the generic
"I gathered some information but had trouble putting it all together"
fallback (conv 2e34ab0f, "what you can do for me").

Fix: memory cap raised 1 → 3, and the granted pass is now genuinely
tool-free (_loop_guard_tool_free → tools=[], tool_choice="none").
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.routers.agents import (  # noqa: E402
    TOOL_CALL_CAPS,
    _detect_tool_call_loop,
)


def _tc(name: str, args: dict, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, sort_keys=True)},
    }


def _memory_turn(msgs: list, turn: int, args: dict | None = None) -> None:
    call_id = f"call_mem_{turn}"
    a = args or {"action": "save", "content": f"note {turn}"}
    msgs.append({"role": "user", "content": f"turn {turn}"})
    msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [_tc("memory", a, call_id)],
    })
    msgs.append({
        "role": "tool",
        "content": json.dumps({"success": True}),
        "tool_call_id": call_id,
    })
    msgs.append({"role": "assistant", "content": "saved."})


def test_memory_cap_raised():
    assert TOOL_CALL_CAPS["memory"] == 3


def test_single_memory_call_does_not_trip():
    msgs = []
    _memory_turn(msgs, 1)
    assert _detect_tool_call_loop(msgs) is None


def test_two_identical_memory_calls_do_not_trip():
    msgs = []
    args = {"action": "save", "content": "the user prefers english"}
    _memory_turn(msgs, 1, args)
    _memory_turn(msgs, 2, args)
    assert _detect_tool_call_loop(msgs) is None


def test_three_identical_memory_calls_trip():
    msgs = []
    args = {"action": "save", "content": "same note"}
    _memory_turn(msgs, 1, args)
    _memory_turn(msgs, 2, args)
    _memory_turn(msgs, 3, args)
    info = _detect_tool_call_loop(msgs)
    assert info is not None
    assert info[0] == "memory"


def test_realistic_capability_turn_does_not_trip():
    """The exact failing shape: skills_hub once + memory once → no trip."""
    msgs = [{"role": "user", "content": "what you can do for me"}]
    msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [
            _tc("skills_hub", {"action": "search", "query": "capabilities"}, "c1"),
            _tc("memory", {"action": "save", "content": "user asked capabilities"}, "c2"),
        ],
    })
    msgs.append({
        "role": "tool", "content": json.dumps({"success": True}),
        "tool_call_id": "c1",
    })
    msgs.append({
        "role": "tool", "content": json.dumps({"success": True}),
        "tool_call_id": "c2",
    })
    assert _detect_tool_call_loop(msgs) is None
