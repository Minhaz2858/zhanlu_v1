"""Fix 1c — rebuttal detection for the dimension-coverage detector.

When the assistant explicitly rebuts a candidate dimension ("no such column in
the catalog", "not available"), the token is NOT a phantom flag — the model
already explained why the dimension cannot be returned. Rebuttal only
suppresses; it never introduces new flags.
"""
import pytest

from app.services import answer_verification as av


def test_rebuttal_suppresses_missing_flag():
    """'no such column' near the missing token → the token is dropped from the
    flagged missing list."""
    missing = av._detect_dimension_coverage(
        "show me sales by region",
        [{"tool": "execute_query", "columns": ["sales_amount"],
          "rows": [{"sales_amount": 3200}]}],
        "Region is not available: there is no such column 'region' in the "
        "catalog, so I returned total sales instead.",
    )
    assert missing == []


def test_no_rebuttal_keeps_flag():
    """Without any rebuttal, a genuinely missing dimension is still flagged."""
    missing = av._detect_dimension_coverage(
        "show me sales by region",
        [{"tool": "execute_query", "columns": ["sales_amount"],
          "rows": [{"sales_amount": 3200}]}],
        "Total sales: 3200.",
    )
    assert missing == ["region"]


def test_partial_rebuttal_only_suppresses_rebutted_token():
    """Two missing dimensions, one rebutted → only the rebutted one is dropped."""
    missing = av._detect_dimension_coverage(
        "show me sales by region and customer",
        [{"tool": "execute_query", "columns": ["sales_amount"],
          "rows": [{"sales_amount": 3200}]}],
        "I checked the catalog and there is no such column 'region' available "
        "for grouping, so I returned total sales instead. Total sales: 3200.",
    )
    assert missing == ["customer"]


def test_is_rebutted_patterns():
    """The helper matches each documented rebuttal phrase case-insensitively."""
    for phrase in [
        "x is not in the catalog",
        "x is not in catalog",
        "no such dimension 'x'",
        "no such column 'x'",
        "no such field 'x'",
        "no such attribute 'x'",
        "x doesn't exist",
        "x does not exist",
        "x is not available",
        "x is not present",
        "none of the dimensions include x",
    ]:
        assert av._is_rebutted("x", phrase), f"expected rebuttal for: {phrase}"


def test_is_rebutted_window_limits_false_positives():
    """A rebuttal two sentences away from the token is not a rebuttal of it."""
    text = ("The sales report is complete and totals 3200. "
            "Note that no such column 'region' exists in the catalog.")
    assert av._is_rebutted("report", text) is False


def test_is_rebutted_empty_inputs():
    assert av._is_rebutted("", "no such column") is False
    assert av._is_rebutted("x", "") is False
    assert av._is_rebutted("x", None) is False
