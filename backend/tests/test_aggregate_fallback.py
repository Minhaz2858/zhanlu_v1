"""2026-08-26: Test for the aggregate-based fallback narrative.

User complaint: when the synthesis LLM call fails on a report-style
request, the user sees ONLY 'Analyzing N rows of data…' — a useless
placeholder. The fix: _data_rows_fallback() now builds a real
narrative from the row aggregates when the user asked for a report
AND rows are available.
"""
import pytest


def test_aggregate_fallback_returns_narrative_for_report_request():
    """When user asked for a report AND rows exist, the fallback must
    return a real written narrative, NOT 'Analyzing N rows…'."""
    from app.routers import agents as a
    # Build a small tool-call shape that the extractor understands
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {
            "rows": [
                {"customer": "A", "revenue": 100, "margin": 10.0},
                {"customer": "B", "revenue": 200, "margin": 20.0},
                {"customer": "C", "revenue": 300, "margin": 30.0},
            ],
        },
    }]
    user_msg = "give me Contract Performance for last month report"
    out = a._data_rows_fallback(tool_calls, user_content=user_msg)
    # Must NOT be the useless placeholder
    assert not out.startswith("Analyzing"), f"got placeholder: {out!r}"
    # Must be a real narrative
    assert "Report Summary" in out or "Key Numbers" in out
    # Must include some real numbers
    assert "100" in out or "200" in out or "300" in out
    # Must suggest next steps
    assert "Next Steps" in out or "next" in out.lower()


def test_aggregate_fallback_keeps_placeholder_for_non_report_request():
    """When user did NOT ask for a report, keep the existing
    'Analyzing N rows…' placeholder behavior (don't change UX
    for non-report turns)."""
    from app.routers import agents as a
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {
            "rows": [
                {"customer": "A", "revenue": 100},
            ],
        },
    }]
    user_msg = "how many customers do we have"
    out = a._data_rows_fallback(tool_calls, user_content=user_msg)
    # Non-report request → placeholder OK
    assert out.startswith("Analyzing")


def test_aggregate_fallback_handles_empty_user_msg():
    """Defensive: no user_content should not crash, falls back to
    the placeholder."""
    from app.routers import agents as a
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {
            "rows": [
                {"customer": "A", "revenue": 100},
            ],
        },
    }]
    out = a._data_rows_fallback(tool_calls)
    # Should be a string (placeholder or aggregate narrative)
    assert isinstance(out, str)
    assert len(out) > 0


def test_aggregate_fallback_with_more_rows_includes_top_performers():
    """When there are enough rows, the narrative should include
    'Top Performers' section listing the highest-value rows."""
    from app.routers import agents as a
    rows = [
        {"customer": f"Customer{i:02d}", "revenue": 100 * (i + 1)}
        for i in range(10)
    ]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows},
    }]
    user_msg = "performance report"
    out = a._data_rows_fallback(tool_calls, user_content=user_msg)
    assert "Top Performers" in out, f"missing top performers section: {out!r}"
    # The highest-revenue customer should appear
    assert "Customer09" in out