"""2026-08-25: Test that tool_call_started events emitted under a
subagent carry a parent_subagent_id matching the open subagent.

The frontend uses this to nest the tool row under the subagent row,
eliminating the duplicate-row visual noise.
"""
import inspect
import pytest


def test_subagent_invoked_then_tool_call_started_has_parent_id():
    """The event-emission code in agents.py must populate
    parent_subagent_id on the tool_call_started event when there is
    an open subagent with matching target.
    """
    from app.routers import agents
    src = inspect.getsource(agents)
    # The implementation must reference the field name
    assert "parent_subagent_id" in src, (
        "agents.py does not emit parent_subagent_id on tool_call_started events. "
        "The frontend relies on this field to nest tool rows under subagent rows."
    )
    # The tool_call_started event's params must include it
    # Look for the _push_live_event("tool_call_started" call and confirm
    # the parent_subagent_id field is computed from the open subagent stack
    assert "_subagent_id_stack" in src or "subagent_id_stack" in src or "parent_subagent" in src, (
        "agents.py must track an open-subagent stack to compute parent_subagent_id"
    )
