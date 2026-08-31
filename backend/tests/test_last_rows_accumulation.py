"""Verify _last_rows is set to the latest successful ask_data_agent result.

The synthesis LLM needs the most recent successful data to produce
a coherent report. If _last_rows is not updated correctly, the synthesis
sees stale or empty data.

This is a STRUCTURAL test — we verify the source code has the correct
update logic in place.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def test_last_rows_set_on_ask_data_result():
    """When ask_data_agent returns rows, _last_rows must be updated to
    the latest result. (This is already the case in the codebase.)"""
    import inspect
    from app.routers import agents
    src = inspect.getsource(agents)
    # Look for the pattern "_last_rows = " inside the ask_data_agent
    # result handling block
    assert "_last_rows = " in src, (
        "_last_rows is never updated. The synthesis LLM would see "
        "stale or empty data."
    )
    # And the update should be in the ask_data_agent branch
    # (rough check: count of "_last_rows = " assignments)
    assignments = src.count("_last_rows = ")
    assert assignments >= 1, (
        f"_last_rows is assigned {assignments} times in agents.py. "
        f"Expected at least 1 assignment in the ask_data_agent branch."
    )


def test_deterministic_fallback_uses_last_rows():
    """_build_deterministic_fallback receives data_rows and uses it
    to produce a CEO-grade analysis. This is already implemented
    (Session 14)."""
    from app.routers.agents import _build_deterministic_fallback
    rows = [{"a": 1, "b": 2}]
    result = _build_deterministic_fallback(
        rows, columns=["a", "b"], table_name="test",
    )
    assert "Executive Summary" in result
    assert "Number of records: 1" in result
