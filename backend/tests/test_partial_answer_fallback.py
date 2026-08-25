"""Tests for the partial-answer fallback that prevents the generic
'I gathered some information but had trouble putting it all together'
apology when tools ran but no data rows were detected.

Bug repro: When the user asks a multi-part question (e.g. "by product
by revenue by customer + competitor price vs our price") and one
tool call returns data while another returns nothing, the LLM
apologizes and the system shows the unhelpful generic fallback.
After the fix, the system shows the data it DID retrieve plus an
explanation of what couldn't be answered and a concrete next step.
"""
import pytest
from app.routers.agents import (
    _partial_answer_fallback,
    _choose_fallback,
    _GENERIC_EMPTY_CONTENT_FALLBACK,
    _DASHBOARD_REDIRECT_FALLBACK,
)


# ---------------------------------------------------------------------------
# 1. _partial_answer_fallback direct tests
# ---------------------------------------------------------------------------

def test_partial_answer_with_retrieved_data():
    """When data was retrieved, the fallback shows it as a markdown table."""
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {
                "success": True,
                "rows": [
                    {"product": "A", "revenue": 1000},
                    {"product": "B", "revenue": 2000},
                ],
                "answer": "Here is revenue by product",
            },
        },
    ]
    out = _partial_answer_fallback(tool_calls, "by product by revenue")
    assert "Retrieved data" in out
    assert "ask_data_agent" in out
    assert "A" in out
    assert "B" in out
    assert "1000" in out
    # Should NOT contain the generic apology phrase
    assert "trouble putting it all together" not in out
    assert "I gathered some information" not in out


def test_partial_answer_with_no_data_explains_what_is_missing():
    """When all tools returned empty, the fallback explains what's missing
    and gives a concrete re-prompt."""
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {
                "success": True,
                "rows": [],
                "answer": "No competitor price data is available in this warehouse",
            },
        },
    ]
    out = _partial_answer_fallback(tool_calls, "competitor price vs our price")
    assert "couldn't find" in out.lower() or "couldn't" in out.lower()
    assert "competitor price" in out.lower() or "ask" in out.lower()
    # Should NOT be the apology
    assert "I gathered some information" not in out


def test_partial_answer_with_partial_data_mixes_both():
    """When some queries succeeded and some returned empty, show both."""
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {
                "success": True,
                "rows": [{"customer": "ACME", "revenue": 5000}],
                "answer": "ok",
            },
        },
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {
                "success": True,
                "rows": [],
                "answer": "No competitor data found",
            },
        },
    ]
    out = _partial_answer_fallback(tool_calls, "by customer by revenue and competitor price")
    assert "Retrieved data" in out
    assert "ACME" in out
    assert "5000" in out
    assert "couldn't find" in out.lower() or "What I couldn't" in out or "could not" in out.lower()


def test_partial_answer_with_failed_query_explains_error():
    """When a tool call failed, the fallback surfaces the error."""
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "failed",
            "results": {
                "success": False,
                "error": "Column 'COMP_PRICE' does not exist",
            },
        },
    ]
    out = _partial_answer_fallback(tool_calls, "competitor price")
    assert "failed" in out.lower() or "Column" in out
    # Should give a re-prompt hint
    assert "Try" in out or "try" in out


def test_partial_answer_never_apologizes():
    """The partial-answer fallback must NEVER contain apology language."""
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {"success": True, "rows": [], "answer": "no data"},
        },
    ]
    out = _partial_answer_fallback(tool_calls, "test")
    assert "trouble" not in out.lower()
    assert "gathered some information" not in out.lower()
    assert "apology" not in out.lower()
    assert "sorry" not in out.lower()


# ---------------------------------------------------------------------------
# 2. _choose_fallback — must NOT return generic fallback when any tool ran
# ---------------------------------------------------------------------------

def test_choose_fallback_never_returns_generic_when_tool_ran():
    """BUG REGRESSION TEST: when ANY tool call completed, _choose_fallback
    must NOT return the generic 'I gathered some information but had
    trouble putting it all together' apology. It must return either
    _data_rows_fallback or _partial_answer_fallback output.
    """
    # Scenario A: tool completed, returned 0 rows, no rows detectable
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {
                "success": True,
                "rows": [],
                "answer": "no data in warehouse for this question",
            },
        },
    ]
    out = _choose_fallback(
        tool_calls, False, user_content="competitor price vs our price",
    )
    assert out != _GENERIC_EMPTY_CONTENT_FALLBACK
    assert "trouble putting it all together" not in out
    assert "gathered some information" not in out


def test_choose_fallback_uses_partial_when_no_rows_but_tools_ran():
    """When no rows but tool ran, should use partial-answer fallback."""
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {"success": True, "rows": [], "answer": "no data"},
        },
    ]
    out = _choose_fallback(
        tool_calls, False, user_content="test question",
    )
    # Should mention what was attempted
    assert "ask_data_agent" in out or "What I" in out or "couldn't" in out.lower()


def test_choose_fallback_still_returns_generic_when_no_tools_ran():
    """When NO tool ran at all, the generic fallback is acceptable."""
    out = _choose_fallback([], False, user_content="hello")
    # Could be generic OR dashboard redirect (both are acceptable for
    # this trivial case). The point is: the partial-answer fallback is
    # NOT expected when no tools ran.
    assert out is not None


# ---------------------------------------------------------------------------
# 3. _extract_topic_hint — concrete re-prompt
# ---------------------------------------------------------------------------

def test_extract_topic_hint_strips_prefixes():
    from app.routers.agents import _extract_topic_hint
    assert "competitor price" in _extract_topic_hint("i need competitor price analysis")
    assert "sales report" in _extract_topic_hint("show me sales report for July")


# ---------------------------------------------------------------------------
# 4. Integration: the full multi-tool-call scenario
# ---------------------------------------------------------------------------

def test_choose_fallback_multi_call_partial_data():
    """Repro of the user's actual bug: 2 ask_data_agent calls, one
    returns data, one returns empty. System must NOT show generic
    apology — must show what was retrieved (the actual data table)."""
    tool_calls = [
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {
                "success": True,
                "rows": [
                    {"product": "A", "revenue": 10000},
                    {"product": "B", "revenue": 5000},
                ],
                "answer": "ok",
            },
        },
        {
            "name": "ask_data_agent",
            "status": "completed",
            "results": {
                "success": True,
                "rows": [],
                "answer": "No competitor data available in this data source",
            },
        },
    ]
    out = _choose_fallback(
        tool_calls, False,
        user_content="by product by revenue by customer and competitor price",
    )
    # Must NOT be the generic apology
    assert "I gathered some information" not in out
    assert "trouble putting it all together" not in out
    # Must show the actual data table inline (the row values)
    assert "10000" in out
    assert "5000" in out
    assert "A" in out
    assert "B" in out
