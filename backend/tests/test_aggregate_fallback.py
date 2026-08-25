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
    # Must be a real narrative with business-scenario terms
    assert "Performance Report" in out, f"missing scenario header: {out!r}"
    # Must use business terms (not just column names)
    assert "Total Contract Value" in out, f"missing business term: {out!r}"
    # Must include some real numbers
    assert "100" in out or "200" in out or "300" in out
    # Must suggest next steps
    assert "Next Steps" in out or "next" in out.lower()
    # Must NOT expose the "synthesis service did not complete" text
    assert "synthesis service" not in out.lower()


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


# ── 2026-08-26 round-3: business-scenario awareness ────────────────

def test_id_columns_excluded_from_key_numbers():
    """customer_id and contract_id are IDs, not business metrics.
    They must NOT appear in the headline/Key Numbers sections."""
    from app.routers import agents as a
    rows = [
        {
            "contract_id": 100001,
            "customer_id": 555,
            "total_contract_amount": 100_000,
            "margin_amount": 10_000,
        },
        {
            "contract_id": 100002,
            "customer_id": 666,
            "total_contract_amount": 200_000,
            "margin_amount": 20_000,
        },
    ]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows},
    }]
    out = a._data_rows_fallback(
        tool_calls,
        user_content="give me Contract Performance for last month report",
    )
    # The narrative should mention the business term, not the raw column
    assert "Total Contract Value" in out
    # Must NOT include the raw ID columns in Key Numbers
    # (they may appear in Top Performers, but the metric must be the
    # business column, not the ID itself)
    assert "100,001" not in out  # contract_id as number
    # Make sure money metric is in the headline
    assert "300,000" in out or "¥30.00万" in out or "¥300" in out


def test_contract_scenario_uses_contract_vocabulary():
    """When user asks for 'Contract Performance', the narrative must
    use contract-specific terms (Total Contract Value, etc.)."""
    from app.routers import agents as a
    rows = [
        {"customer": "Acme", "total_contract_amount": 500_000, "margin_amount": 50_000}
        for _ in range(3)
    ]
    tool_calls = [{"name": "ask_data_agent", "results": {"rows": rows}}]
    out = a._data_rows_fallback(
        tool_calls,
        user_content="give me Contract Performance for last month report",
    )
    # Scenario-specific terms
    assert "Total Contract Value" in out
    assert "Average Contract Value" in out
    # No "synthesis service" internal text
    assert "synthesis service" not in out.lower()


def test_sales_scenario_uses_sales_vocabulary():
    """When user asks for 'sales report', the narrative uses sales terms."""
    from app.routers import agents as a
    rows = [
        {"customer": "Acme", "revenue": 100_000, "profit": 10_000}
        for _ in range(2)
    ]
    tool_calls = [{"name": "ask_data_agent", "results": {"rows": rows}}]
    out = a._data_rows_fallback(
        tool_calls, user_content="give me sales report for last month",
    )
    assert "Total Sales" in out or "Total Revenue" in out or "Total" in out
    # Must use business terms, not raw column names
    # (column name `revenue` may appear but only inside the metric list)


def test_chinese_report_request_uses_chinese_terms():
    """When user asks in Chinese, the narrative uses Chinese business terms."""
    from app.routers import agents as a
    rows = [
        {"客户": "A", "合同金额": 100_000, "毛利": 10_000}
        for _ in range(2)
    ]
    tool_calls = [{"name": "ask_data_agent", "results": {"rows": rows}}]
    out = a._data_rows_fallback(
        tool_calls, user_content="给我上月合同绩效报告",
    )
    # Chinese scenario terms
    assert "绩效报告" in out or "合同" in out
    assert "合同总金额" in out or "金额" in out


def test_activity_by_day_uses_business_terms():
    """When a date column exists, the narrative includes peak/slowest day
    in business terms (not just raw date strings)."""
    from app.routers import agents as a
    rows = [
        {
            "customer": "A", "total_contract_amount": 1000,
            "contract_date": "2026-07-01T00:00:00",
        },
        {
            "customer": "B", "total_contract_amount": 5000,
            "contract_date": "2026-07-15T00:00:00",
        },
        {
            "customer": "C", "total_contract_amount": 200,
            "contract_date": "2026-07-25T00:00:00",
        },
    ]
    tool_calls = [{"name": "ask_data_agent", "results": {"rows": rows}}]
    out = a._data_rows_fallback(
        tool_calls, user_content="contract report last month",
    )
    assert "Activity by Day" in out or "Peak day" in out
    assert "2026-07-15" in out  # the peak day should be 7-15 with highest value
    assert "2026-07-25" in out  # the slowest day


def test_recommendations_are_scenario_specific():
    """The recommendations section should mention the user's business
    scenario (e.g. 'top contracts' for contract scenario)."""
    from app.routers import agents as a
    rows = [
        {"customer": f"C{i}", "total_contract_amount": 100 * i}
        for i in range(1, 6)
    ]
    tool_calls = [{"name": "ask_data_agent", "results": {"rows": rows}}]
    out = a._data_rows_fallback(
        tool_calls, user_content="contract performance report",
    )
    # Should reference contracts in recommendations
    assert "contracts" in out.lower() or "contract" in out.lower()
    # Should have "Next Steps" or "Recommended"
    assert "Next Steps" in out or "Recommended" in out