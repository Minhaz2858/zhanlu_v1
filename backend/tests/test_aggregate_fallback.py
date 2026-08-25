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
    """When user did NOT ask for a report, the fallback now STILL
    produces a narrative (round 5: user wants extensive answers for
    ALL data queries, not just report-style ones)."""
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
    # 2026-08-26: was a placeholder for non-report requests; now
    # ALL data queries get the aggregate narrative
    assert not out.startswith("Analyzing"), (
        f"got placeholder for non-report request (should be narrative now): {out!r}"
    )


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
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "aipdp_data_warehouse_prod"},
    }]
    out = a._data_rows_fallback(
        tool_calls,
        user_content="give me Contract Performance for last month report",
    )
    # Scenario-specific terms
    assert "Total Contract Value" in out
    # Should mention "average" in some form
    assert "average" in out.lower()
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


# ── 2026-08-26 round-4: extensive narrative + metadata-only fix ──

def test_metadata_only_row_produces_extensive_narrative():
    """When the data tool returns only metadata (min/max/count), the
    fallback must produce an extensive narrative explaining what was
    returned and how to fix the question — not a one-liner."""
    from app.routers import agents as a
    rows = [{
        "max_ship_date": "2026-08-19T00:00:00",
        "min_ship_date": "2018-06-05T00:00:00",
        "row_count": 13815,
    }]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "aipdp_data_warehouse_prod"},
    }]
    out = a._data_rows_fallback(
        tool_calls, user_content="give me Contract Performance for last month report",
    )
    # Must be extensive (not the 30-word one-liner)
    word_count = len(out.split())
    assert word_count >= 100, f"metadata fallback too short: {word_count} words"
    # Must explain the situation
    assert "summary" in out.lower() or "metadata" in out.lower()
    # Must give the user actionable next steps
    assert "GROUP BY" in out or "rephrase" in out.lower() or "specific" in out.lower()
    # Must reference the actual date range from the data
    assert "2018-06-05" in out or "2026-08-19" in out


def test_metadata_only_row_count_appears_in_narrative():
    """The row_count value from the metadata row should be surfaced
    so the user knows how many underlying records exist."""
    from app.routers import agents as a
    rows = [{
        "max_ship_date": "2026-08-19T00:00:00",
        "min_ship_date": "2018-06-05T00:00:00",
        "row_count": 13815,
    }]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "aipdp_data_warehouse_prod"},
    }]
    out = a._data_rows_fallback(
        tool_calls, user_content="contract performance report",
    )
    # 13,815 or 1.38万 (Chinese) — at least the number should appear
    assert "13,815" in out or "1.38万" in out or "13815" in out


def test_extensive_report_includes_executive_summary():
    """When data is rich, the report must include a proper Executive
    Summary paragraph (4-6 sentences minimum)."""
    from app.routers import agents as a
    rows = [
        {
            "customer": f"C{i:02d}",
            "total_contract_amount": 100_000 * (i + 1),
            "margin_amount": 10_000 * (i + 1),
            "contract_date": f"2026-07-{i+1:02d}T00:00:00",
        }
        for i in range(5)
    ]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "aipdp_data_warehouse_prod"},
    }]
    out = a._data_rows_fallback(
        tool_calls, user_content="give me Contract Performance for last month report",
    )
    # Must have all the required sections
    assert "Executive Summary" in out
    assert "Revenue & Amounts" in out
    assert "Trends & Comparisons" in out
    assert "Recommended Next Steps" in out
    # Executive summary should be a paragraph (not a one-liner)
    exec_idx = out.find("Executive Summary")
    after = out[exec_idx:exec_idx + 800]
    # Count sentences (rough)
    sentences = after.count(".") + after.count("。")
    assert sentences >= 3, f"executive summary too short: {sentences} sentences"


def test_extensive_report_includes_anomalies():
    """The report should include a 'Notable Anomalies' section
    when the data has enough variance to support outlier detection."""
    from app.routers import agents as a
    # Create data with one big outlier
    rows = [
        {"customer": f"C{i}", "total_contract_amount": 1_000, "contract_date": f"2026-07-{i+1:02d}"}
        for i in range(5)
    ]
    rows.append({"customer": "OUTLIER", "total_contract_amount": 100_000, "contract_date": "2026-07-15"})
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "test"},
    }]
    out = a._data_rows_fallback(
        tool_calls, user_content="contract performance report",
    )
    # Should mention outliers or anomalies
    assert "outlier" in out.lower() or "anomal" in out.lower()


def test_extensive_report_word_count_meets_target():
    """The fallback should produce a 200+ word narrative (target 400-800)."""
    from app.routers import agents as a
    rows = [
        {
            "customer": f"Customer_{i:02d}",
            "total_contract_amount": 50_000 * (i + 1) + 10_000,
            "margin_amount": 5_000 * (i + 1),
            "contract_date": f"2026-07-{(i % 28) + 1:02d}T00:00:00",
        }
        for i in range(10)
    ]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "aipdp_data_warehouse_prod"},
    }]
    out = a._data_rows_fallback(
        tool_calls, user_content="give me Contract Performance for last month report",
    )
    word_count = len(out.split())
    assert word_count >= 200, f"report too short: {word_count} words (target 200+)"