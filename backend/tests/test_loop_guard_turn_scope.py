"""Behavioral tests for turn-scoped loop detection in
``_detect_tool_call_loop``.

Bug: the guard scanned the ENTIRE conversation history (all turns).
A legitimate cross-turn repetition — e.g. the user clicking "Run Now"
on the same automation task every day, so the LLM calls
``execute_automation(name="Daily Sales Data Sync")`` once per turn —
accumulated across turns and eventually tripped the in-turn loop guard
on iteration 0 of a fresh turn. The turn then broke out of the tool
loop before ``content_streamed`` was initialized, crashing the v3 SSE
generator with ``UnboundLocalError`` ("Sorry, the connection was
interrupted" in the chat UI).

Fix: an optional ``start_idx`` parameter scopes the scan to messages
from the current turn onward. The default (0) preserves the original
history-wide behavior for the other call sites and existing tests.
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _tc(name: str, args: dict, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, sort_keys=True),
        },
    }


def _run_automation_turn(msgs: list, turn: int) -> None:
    """Append one prior turn in which the LLM successfully called
    execute_automation with identical args (the Run-Now flow)."""
    call_id = f"call_run_{turn}"
    msgs.append({"role": "user", "content": f"Run Automation Task (turn {turn})"})
    msgs.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [_tc("execute_automation", {"name": "Daily Sales Data Sync"}, call_id)],
    })
    msgs.append({
        "role": "tool",
        "content": json.dumps({"success": True, "status": "Running"}),
        "tool_call_id": call_id,
    })
    msgs.append({"role": "assistant", "content": "The task has been triggered."})


def test_history_wide_scan_still_trips_by_default():
    """Default behavior is unchanged: with no ``start_idx`` the guard
    scans the whole history and trips after cap successful identical
    calls (cap = 10)."""
    from app.routers.agents import _detect_tool_call_loop

    msgs = []
    for t in range(10):
        _run_automation_turn(msgs, t)
    assert _detect_tool_call_loop(msgs) is not None, (
        "History-wide default scan should still trip on 10 identical "
        "successful calls."
    )


def test_turn_scoped_scan_ignores_prior_turns():
    """With ``start_idx`` pointing at the current turn's user message,
    10 identical successful calls from PRIOR turns must NOT trip the
    guard — the user legitimately re-ran the same automation once per
    turn."""
    from app.routers.agents import _detect_tool_call_loop

    msgs = []
    for t in range(10):
        _run_automation_turn(msgs, t)
    # Current turn begins: the user's fresh "Run Now" message.
    msgs.append({"role": "user", "content": "Run Automation Task (turn 11)"})
    turn_start = len(msgs) - 1
    assert _detect_tool_call_loop(msgs, start_idx=turn_start) is None, (
        "Turn-scoped guard must not trip on prior-turn repetitions — "
        "re-running the same automation across turns is legitimate."
    )


def test_turn_scoped_scan_still_catches_in_turn_loop():
    """Within the CURRENT turn, 11 identical FAILED calls (cap+1 for
    failures) must still trip the guard — in-turn retry loops are the
    guard's actual purpose."""
    from app.routers.agents import _detect_tool_call_loop

    msgs = [{"role": "user", "content": "do the thing"}]
    turn_start = len(msgs) - 1
    for i in range(11):
        call_id = f"c{i}"
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [_tc("execute_automation", {"name": "X"}, call_id)],
        })
        msgs.append({
            "role": "tool",
            "content": json.dumps({"success": False, "error": "boom"}),
            "tool_call_id": call_id,
        })
    info = _detect_tool_call_loop(msgs, start_idx=turn_start)
    assert info is not None, (
        "Turn-scoped guard must still trip on an in-turn retry loop."
    )
    name, _count = info
    assert name == "execute_automation"
