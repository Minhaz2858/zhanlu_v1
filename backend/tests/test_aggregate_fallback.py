"""2026-08-26: Test for the generic, column-driven aggregate fallback.

The previous version used hardcoded business-scenario detection
(contract/sales/shipment/inventory/production/pricing) which
failed for any domain outside that list (HR, medical, education,
etc.). The new version is GENERIC — it derives all labels from
the actual column names, so it works for ANY database.
"""
import pytest


def test_aggregate_fallback_returns_narrative_for_report_request():
    """When user asked for a report AND rows exist, the fallback must
    return a real written narrative, NOT 'Analyzing N rows…'."""
    from app.routers import agents as a
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {
            "rows": [
                {"customer": "A", "revenue": 100, "margin": 10.0},
                {"customer": "B", "revenue": 200, "margin": 20.0},
                {"customer": "C", "revenue": 300, "margin": 30.0},
            ],
            "source_name": "test_db",
        },
    }]
    user_msg = "give me Contract Performance for last month report"
    out = a._data_rows_fallback(tool_calls, user_content=user_msg)
    assert not out.startswith("Analyzing"), f"got placeholder: {out!r}"
    assert "Data Report" in out or "Performance Report" in out
    assert "100" in out or "200" in out or "300" in out
    assert "Next Steps" in out or "next" in out.lower()
    assert "synthesis service" not in out.lower()


def test_aggregate_fallback_handles_empty_user_msg():
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
    assert isinstance(out, str)
    assert len(out) > 0


def test_aggregate_fallback_with_more_rows_includes_top_performers():
    from app.routers import agents as a
    rows = [
        {"customer": f"Customer{i:02d}", "revenue": 100 * (i + 1)}
        for i in range(10)
    ]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "test"},
    }]
    user_msg = "performance report"
    out = a._data_rows_fallback(tool_calls, user_content=user_msg)
    assert "Top Performers" in out
    assert "Customer09" in out


# ── 2026-08-26 round-7: GENERIC, no scenario-specific terms ─────

def test_humanize_column_name():
    """snake_case → Title Case for the fallback label."""
    from app.routers import agents as a
    assert a._humanize_column_name("total_contract_amount") == "Total Contract Amount"
    assert a._humanize_column_name("revenue") == "Revenue"
    assert a._humanize_column_name("customer_id") == "Customer"  # _id stripped
    assert a._humanize_column_name("product_name") == "Product Name"
    assert a._humanize_column_name("unit_price") == "Unit Price"
    assert a._humanize_column_name("") == ""
    # Chinese pass-through
    assert a._humanize_column_name("客户") == "客户"


def test_no_hardcoded_business_scenario_patterns():
    """The old _BUSINESS_SCENARIO_PATTERNS and _detect_business_scenario
    must be removed — those were the keyword-based logic that broke
    for HR/medical/education/any non-business database."""
    from app.routers import agents as a
    assert not hasattr(a, "_detect_business_scenario"), (
        "_detect_business_scenario still exists — should be removed"
    )
    assert not hasattr(a, "_BUSINESS_SCENARIO_PATTERNS"), (
        "_BUSINESS_SCENARIO_PATTERNS still exists — hardcoded scenarios "
        "break for HR/medical/education/any non-business database"
    )


def test_id_columns_excluded_from_key_numbers():
    """customer_id and contract_id are IDs, not business metrics."""
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
        "results": {"rows": rows, "source_name": "test"},
    }]
    out = a._data_rows_fallback(
        tool_calls,
        user_content="give me Contract Performance for last month report",
    )
    # Column-name-derived label
    assert "Total Contract Amount" in out
    # Money in headline
    assert "300,000" in out or "¥30.00万" in out or "¥300" in out


def test_works_for_arbitrary_database_columns():
    """The fallback must work for any database schema, not just
    'contract' or 'sales' columns. Test with HR/medical style columns."""
    from app.routers import agents as a
    # HR database
    hr_rows = [
        {"employee_name": "Alice", "salary": 80000, "tenure_months": 36},
        {"employee_name": "Bob", "salary": 95000, "tenure_months": 48},
        {"employee_name": "Carol", "salary": 72000, "tenure_months": 12},
    ]
    tc = [{"name": "ask_data_agent", "results": {"rows": hr_rows, "source_name": "hr_db"}}]
    out = a._data_rows_fallback(tc, user_content="show me employee salary analysis")
    assert not out.startswith("Analyzing")
    # Should mention the column-derived label
    assert "Salary" in out
    assert "Employee" in out or "employee_name" in out
    # Should NOT use hardcoded scenario terms
    assert "Total Contract Value" not in out
    assert "Total Sales" not in out


def test_chinese_columns_work():
    from app.routers import agents as a
    rows = [
        {"客户": "A", "合同金额": 100_000},
        {"客户": "B", "合同金额": 200_000},
    ]
    tc = [{"name": "ask_data_agent", "results": {"rows": rows, "source_name": "test"}}]
    out = a._data_rows_fallback(tc, user_content="给我合同分析")
    assert "合同金额" in out


def test_recommendations_are_generic():
    from app.routers import agents as a
    rows = [
        {"item": f"X{i}", "value": 100 * i}
        for i in range(1, 6)
    ]
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": "test"},
    }]
    out = a._data_rows_fallback(
        tool_calls, user_content="performance report",
    )
    assert "Next Steps" in out or "Recommended" in out
    assert "Drill" in out or "drill" in out.lower()


# ── 2026-08-26 round-4: extensive narrative + metadata-only fix ──

def test_metadata_only_row_produces_extensive_narrative():
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
    word_count = len(out.split())
    assert word_count >= 100, f"metadata fallback too short: {word_count} words"
    assert "summary" in out.lower() or "metadata" in out.lower()
    assert "GROUP BY" in out or "rephrase" in out.lower() or "specific" in out.lower()
    assert "2018-06-05" in out or "2026-08-19" in out


def test_metadata_only_row_count_appears_in_narrative():
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
    assert "13,815" in out or "1.38万" in out or "13815" in out


def test_extensive_report_includes_executive_summary():
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
    assert "Executive Summary" in out
    assert "Revenue & Amounts" in out or "Margins & Rates" in out
    assert "Trends & Comparisons" in out
    assert "Recommended Next Steps" in out


def test_extensive_report_includes_anomalies():
    from app.routers import agents as a
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
    assert "outlier" in out.lower() or "anomal" in out.lower()


def test_extensive_report_word_count_meets_target():
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


# ── 2026-08-26 round-6: all data queries get a narrative ────────

def test_non_report_data_query_also_gets_narrative():
    """Round 6: even queries without 'report' keyword get a narrative."""
    from app.routers import agents as a
    tool_calls = [{
        "name": "ask_data_agent",
        "results": {
            "rows": [
                {"customer": "A", "revenue": 100},
            ],
        },
    }]
    out = a._data_rows_fallback(tool_calls, user_content="how many customers do we have")
    assert not out.startswith("Analyzing")
