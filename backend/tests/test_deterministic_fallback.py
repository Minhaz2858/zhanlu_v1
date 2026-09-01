"""Tests for the deterministic data-rows fallback.

When LLM synthesis fails (empty/apology/<100chars/timeout), this fallback
renders a useful markdown summary from the data rows — no LLM needed.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_deterministic_fallback.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app.routers.agents import _build_deterministic_fallback, _should_trigger_fallback


def test_fallback_renders_numeric_summary():
    """The fallback computes totals/avg/min/max for numeric columns."""
    rows = [
        {"product": "Widget", "revenue": 1000.0},
        {"product": "Gadget", "revenue": 2000.0},
        {"product": "Widget", "revenue": 1500.0},
    ]
    result = _build_deterministic_fallback(rows, columns=["product", "revenue"], table_name="sales")
    assert "Executive Summary (auto-generated)" in result
    assert "Total revenue" in result
    assert "4,500.00" in result  # 1000+2000+1500
    assert "Number of records: 3" in result


def test_fallback_renders_top_values():
    """The fallback shows top-3 values for categorical columns."""
    rows = [
        {"product": "Widget", "revenue": 1000.0},
        {"product": "Widget", "revenue": 1500.0},
        {"product": "Gadget", "revenue": 2000.0},
    ]
    result = _build_deterministic_fallback(rows, columns=["product", "revenue"], table_name="sales")
    assert "Widget" in result  # top product (2 occurrences)
    assert "Gadget" in result


def test_fallback_renders_date_range():
    """The fallback shows min/max date when a date column is present."""
    rows = [
        {"date": "2026-07-01", "revenue": 1000.0},
        {"date": "2026-07-15", "revenue": 2000.0},
        {"date": "2026-07-31", "revenue": 1500.0},
    ]
    result = _build_deterministic_fallback(rows, columns=["date", "revenue"], table_name="sales")
    assert "2026-07-01" in result
    assert "2026-07-31" in result


def test_fallback_renders_markdown_table():
    """The fallback includes a markdown table of the top 20 rows."""
    rows = [{"product": f"Item{i}", "revenue": float(i * 100)} for i in range(50)]
    result = _build_deterministic_fallback(rows, columns=["product", "revenue"], table_name="sales")
    assert "| product | revenue |" in result or "| product |" in result
    assert "Item0" in result
    # Should NOT include all 50 rows (capped at 20)
    assert "Item20" not in result
    assert "20" in result  # mentioned in "first 20 of 50"


def test_fallback_triggers_on_empty_synthesis():
    """Empty synthesis output triggers the fallback."""
    assert _should_trigger_fallback("", data_rows=[{"x": 1}]) is True


def test_fallback_triggers_on_apology():
    """Apology text triggers the fallback."""
    assert _should_trigger_fallback("I couldn't find the data. Let me re-query...", data_rows=[{"x": 1}]) is True


def test_fallback_triggers_on_short_output():
    """<100 char output triggers the fallback."""
    assert _should_trigger_fallback("No data found.", data_rows=[{"x": 1}]) is True


def test_fallback_does_not_trigger_on_valid_synthesis():
    """Valid 2000-char synthesis does NOT trigger the fallback."""
    valid = "Here is your sales report. " * 100  # ~2600 chars
    assert _should_trigger_fallback(valid, data_rows=[{"x": 1}]) is False


def test_fallback_does_not_trigger_when_no_data():
    """When there's no data, the fallback shouldn't fire (no data to show)."""
    assert _should_trigger_fallback("", data_rows=[]) is False
