"""Test that _should_finalize_no_data respects any_with_rows.

Root cause: the no-data finalize fired on the first 0-row result,
ignoring that a later ask_data_agent call might have data.

Fix: _should_finalize_no_data now takes an `any_with_rows` parameter.
If True, return False — data exists elsewhere in the turn.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def test_should_finalize_no_data_accepts_any_with_rows():
    """_should_finalize_no_data must accept `any_with_rows` keyword arg."""
    import inspect
    from app.routers.agents import _should_finalize_no_data
    sig = inspect.signature(_should_finalize_no_data)
    assert "any_with_rows" in sig.parameters, (
        "_should_finalize_no_data must accept `any_with_rows` keyword. "
        "Current signature: " + str(sig)
    )


def test_should_finalize_no_data_returns_false_when_any_with_rows_true():
    """When any_with_rows=True, the function must return False even
    if the current result is 0 rows + doc_format is set."""
    from app.routers.agents import _should_finalize_no_data
    # Simulate: 0-row result, PPT format, but other call has data
    result = {"success": True, "rows": []}
    doc_format = "pptx"
    decision = _should_finalize_no_data(
        result, doc_format, any_with_rows=True,
    )
    assert decision is False, (
        f"Expected False when any_with_rows=True, got {decision}. "
        f"The PPT no-data finalize would still fire on the first 0-row "
        f"result, ignoring the data from other iterations."
    )


def test_should_finalize_no_data_true_when_all_calls_empty():
    """When any_with_rows=False and the current result is 0 rows +
    doc_format is set, the function should return True (finalize
    the empty artifact)."""
    from app.routers.agents import _should_finalize_no_data
    result = {"success": True, "rows": []}
    doc_format = "pptx"
    decision = _should_finalize_no_data(
        result, doc_format, any_with_rows=False,
    )
    assert decision is True, (
        f"Expected True when all calls empty + doc_format set, got {decision}."
    )


def test_should_finalize_no_data_false_when_data_in_current_call():
    """When the current call has data, the function must return False
    regardless of any_with_rows."""
    from app.routers.agents import _should_finalize_no_data
    result = {"success": True, "rows": [{"a": 1}]}
    doc_format = "pptx"
    decision = _should_finalize_no_data(
        result, doc_format, any_with_rows=False,
    )
    assert decision is False, (
        f"Expected False when current result has rows, got {decision}."
    )
