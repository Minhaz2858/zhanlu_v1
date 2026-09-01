"""Fix 1a — stoplist extension for the dimension-coverage detector.

Regression guard for the live trace (conv 86d8861c): "make a sales overview
PPT for last month" was degraded by phantom dimension flags extracted from the
request's verb/format/time words (make / ppt / overview / last / month). These
tokens describe the REQUEST, not content dimensions the results must enumerate.
"""
import pytest

from app.services import answer_verification as av


# ── "make a sales overview PPT for last month" → zero phantom flags ─────


def test_make_sales_overview_ppt_last_month_has_no_phantom_flags():
    """The only remaining requested token after stoplists is 'sale'; when the
    results carry sales data the detector must NOT flag make/ppt/overview/
    month/last (the pre-fix bug)."""
    results = [{
        "tool": "execute_query",
        "columns": ["FNAME", "sales_amount"],
        "rows": [{"FNAME": "PVC", "sales_amount": 3200},
                 {"FNAME": "C5", "sales_amount": 4100}],
    }]
    missing = av._detect_dimension_coverage(
        "make a sales overview PPT for last month", results,
        "Here is the sales overview for last month.",
    )
    assert missing == []


def test_verbs_never_extracted_as_dimensions():
    """Verbs like 'make'/'create'/'show' describe the action, never a data
    dimension — even when absent from the payload corpus."""
    results = [{"tool": "execute_query", "columns": ["amount"],
                "rows": [{"amount": 1}]}]
    missing = av._detect_dimension_coverage(
        "make me a breakdown", results, "Here it is.",
    )
    assert missing == []


def test_format_words_never_extracted_as_dimensions():
    """ppt/overview/report/summary are deliverable formats, not dimensions.
    The request here carries no content dimension, so nothing may be flagged."""
    results = [{"tool": "execute_query", "columns": ["amount"],
                "rows": [{"amount": 1}]}]
    missing = av._detect_dimension_coverage(
        "build an overview PPT", results, "Done.",
    )
    assert missing == []


def test_time_words_never_extracted_as_dimensions():
    """month/last/year/week bound the query window; they are not dimensions."""
    results = [{"tool": "execute_query", "columns": ["amount"],
                "rows": [{"amount": 1}]}]
    missing = av._detect_dimension_coverage(
        "show totals for last month", results, "Totals below.",
    )
    assert missing == []


def test_stopword_singular_plural_forms():
    """Singular/plural forms of the stoplists are both suppressed
    (normalization is applied before the membership check)."""
    results = [{"tool": "execute_query", "columns": ["amount"],
                "rows": [{"amount": 1}]}]
    missing = av._detect_dimension_coverage(
        "create monthly reports and yearly overviews", results, "Done.",
    )
    assert missing == []


def test_real_dimension_still_flagged_when_absent():
    """Stoplists must not suppress genuine content dimensions: 'region' is a
    real breakdown dimension and is still flagged when the payload lacks it."""
    results = [{"tool": "execute_query", "columns": ["sales_amount"],
                "rows": [{"sales_amount": 3200}]}]
    missing = av._detect_dimension_coverage(
        "show me sales by region", results, "Total sales: 3200.",
    )
    assert missing == ["region"]
