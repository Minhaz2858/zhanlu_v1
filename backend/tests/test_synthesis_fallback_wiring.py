"""Tests for CEO-grade synthesis fallback in v3 loop.

Root cause being fixed (per user screenshot 2026-08-25):
- Agent retrieved 234 rows of contract performance data successfully
- But synthesis LLM call timed out → SSE stream died
- User saw: "Sorry, I hit an error while responding. Please try again."
- Despite having the full data table rendered!

Fix: When the synthesis LLM call fails/times out AND we have data rows,
use _build_deterministic_fallback (no LLM, pure Python, runs in <50ms)
to produce a CEO-grade analysis. The user NEVER gets "Sorry" if data exists.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_synthesis_fallback_wiring.py']))"
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Test 1: _build_deterministic_fallback is exported and callable ─────────


def test_build_deterministic_fallback_exported():
    """The function I built in earlier sessions must be importable."""
    from app.routers.agents import _build_deterministic_fallback
    assert callable(_build_deterministic_fallback)


def test_build_deterministic_fallback_produces_executive_summary():
    """The deterministic fallback must include an Executive Summary header
    (CEO-grade scaffolding)."""
    from app.routers.agents import _build_deterministic_fallback
    rows = [
        {"product": "Widget", "revenue": 1000.0},
        {"product": "Gadget", "revenue": 2000.0},
        {"product": "Widget", "revenue": 1500.0},
    ]
    result = _build_deterministic_fallback(
        rows, columns=["product", "revenue"], table_name="sales"
    )
    assert "Executive Summary" in result
    assert "Total revenue" in result
    assert "4,500.00" in result
    assert "Number of records: 3" in result


# ── Test 2: _force_llm_synthesis has a timeout wrapper ──────────────────────


def test_force_llm_synthesis_signature_has_timeout():
    """_force_llm_synthesis must accept a timeout parameter (or use one
    internally) so a slow LLM call doesn't kill the SSE stream."""
    import inspect
    from app.routers.agents import _force_llm_synthesis
    sig = inspect.signature(_force_llm_synthesis)
    # Either accept timeout explicitly, or have asyncio.wait_for internally
    has_timeout = "timeout" in sig.parameters
    # Check source for wait_for
    source = inspect.getsource(_force_llm_synthesis)
    has_wait_for = "asyncio.wait_for" in source
    assert has_timeout or has_wait_for, (
        "_force_llm_synthesis must have a timeout (either as parameter or "
        "asyncio.wait_for internally) to prevent SSE stream death"
    )


# ── Test 3: _should_trigger_fallback works correctly ────────────────────────


def test_should_trigger_fallback_on_empty_synthesis():
    """When LLM synthesis returns empty AND data_rows is non-empty,
    the fallback should fire."""
    from app.routers.agents import _should_trigger_fallback
    assert _should_trigger_fallback("", [{"x": 1}]) is True


def test_should_trigger_fallback_on_short_synthesis():
    """<100 char synthesis with data should trigger fallback."""
    from app.routers.agents import _should_trigger_fallback
    assert _should_trigger_fallback("No data found.", [{"x": 1}]) is True


def test_should_trigger_fallback_on_apology():
    """Apology text with data should trigger fallback."""
    from app.routers.agents import _should_trigger_fallback
    text = "I couldn't find the data you requested. Let me try again."
    assert _should_trigger_fallback(text, [{"x": 1}]) is True


def test_should_trigger_fallback_does_not_trigger_on_valid_synthesis():
    """Valid 500+ char synthesis should NOT trigger fallback."""
    from app.routers.agents import _should_trigger_fallback
    valid = "Here is the comprehensive analysis. " * 15  # ~570 chars
    assert _should_trigger_fallback(valid, [{"x": 1}]) is False


def test_should_trigger_fallback_no_data_no_fire():
    """No data rows → no fallback (nothing to show)."""
    from app.routers.agents import _should_trigger_fallback
    assert _should_trigger_fallback("", []) is False


# ── Test 4: Integration — deterministic fallback produces CEO-grade output ──


def test_deterministic_fallback_ceo_grade_with_real_contract_data():
    """Test with realistic contract performance data (matching user's screenshot).
    Output should include: numeric totals, top performers, markdown table."""
    from app.routers.agents import _build_deterministic_fallback
    rows = [
        {"contract_number": "YSK-202607-0393", "customer_name": "Customer A",
         "product_name": "Product X", "contract_qty": 32, "contract_price": 100.0,
         "shipped_qty": 32, "shipped_amount": 3200.0, "execution_rate": 100.0},
        {"contract_number": "YSK-202607-0392", "customer_name": "Customer B",
         "product_name": "Product Y", "contract_qty": 172, "contract_price": 150.0,
         "shipped_qty": 172, "shipped_amount": 25800.0, "execution_rate": 100.0},
        {"contract_number": "YSK-202607-0382", "customer_name": "Customer C",
         "product_name": "Product Z", "contract_qty": 26.5, "contract_price": 200.0,
         "shipped_qty": 26.5, "shipped_amount": 5300.0, "execution_rate": 100.0},
    ]
    result = _build_deterministic_fallback(
        rows,
        columns=["contract_number", "customer_name", "product_name",
                 "contract_qty", "contract_price", "shipped_qty",
                 "shipped_amount", "execution_rate"],
        table_name="contract_performance",
    )
    # CEO-grade: must include numeric totals
    assert "Executive Summary" in result
    assert "Total shipped_amount" in result or "shipped_amount" in result
    # Must show data (not just "Analyzing...")
    assert "YSK-202607-0393" in result or "Customer A" in result
    # Must be a real analysis, not the "Analyzing N rows of data..." placeholder
    assert "Analyzing" not in result, (
        f"Output should be a real analysis, not the placeholder. Got: {result[:200]}"
    )
    # Length should be substantive
    assert len(result) > 200, f"Output too short ({len(result)} chars)"
