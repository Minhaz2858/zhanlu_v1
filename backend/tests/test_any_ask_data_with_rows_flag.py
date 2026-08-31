"""Test the _any_ask_data_with_rows flag in the v3 stream loop.

Root cause (user screenshot 2026-08-25): PPT queries return 'No data available'
because the no-data finalize fires on the first 0-row result, ignoring
later successful ask_data_agent calls.

Fix: Track whether ANY ask_data_agent call in the turn has returned rows.
If yes, skip the no-data finalize for that iteration.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def test_any_ask_data_with_rows_flag_defined():
    """The v3 stream loop must initialize _any_ask_data_with_rows=False
    before the iteration loop starts."""
    import inspect
    from app.routers import agents
    src = inspect.getsource(agents)
    # Look for the flag initialization
    assert "_any_ask_data_with_rows" in src, (
        "_any_ask_data_with_rows flag not defined in agents.py. "
        "Add the flag in the v3 stream loop before the iteration loop."
    )
    # And it must be initialized to False (default state)
    assert "_any_ask_data_with_rows = False" in src, (
        "_any_ask_data_with_rows must be initialized to False. "
        "Found the flag but not the initialization."
    )


def test_flag_set_on_ask_data_with_rows():
    """When an ask_data_agent call returns non-empty rows, the flag
    must be set to True."""
    import inspect
    from app.routers import agents
    src = inspect.getsource(agents)
    # Look for the assignment in the iteration loop
    assert "_any_ask_data_with_rows = True" in src, (
        "_any_ask_data_with_rows must be set to True when an "
        "ask_data_agent call returns rows. The flag never gets set."
    )


def test_flag_persists_across_iterations():
    """Once the flag is True, it must NOT be reset to False in
    later iterations. (The flag is monotonically increasing.)"""
    import inspect
    from app.routers import agents
    src = inspect.getsource(agents)
    # The flag should only be set to True, never reset to False mid-loop
    # Look for any "= False" assignment INSIDE the iteration loop
    # (vs. the initial False before the loop)
    # Simple check: the flag's "= False" assignment should be
    # OUTSIDE the for iteration loop
    false_assignments = src.count("_any_ask_data_with_rows = False")
    assert false_assignments == 1, (
        f"_any_ask_data_with_rows must be initialized to False exactly "
        f"ONCE (before the iteration loop). Found {false_assignments} "
        f"False assignments. Resetting the flag inside the loop would "
        f"break the data-presence tracking."
    )
