"""Tests for the ``is_metadata_only_rows`` shape detector.

The detector is shape-only (Rule 2): it inspects column NAMES, never their
values, and never any business vocabulary. It flags results whose EVERY
column is a MIN/MAX/COUNT aggregate over at most 2 rows — the signature of a
query that silently degraded to a metadata answer (date range / row count)
instead of returning business data.
"""

from __future__ import annotations

import pytest

from app.services.goal_contract import is_metadata_only_rows


# ── True: metadata-only shapes ────────────────────────────────────────────


@pytest.mark.parametrize(
    "rows",
    [
        # ERP-style prefix columns (observed failure: MIN_FDATE / MAX_FDATE).
        [{"MIN_FDATE": "2026-01-01", "MAX_FDATE": "2026-08-19"}],
        [{"min_date": "2026-01-01", "max_date": "2026-08-19"}],
        [{"count_rows": 42}],
        # Function-call form, as returned by pandas/db clients.
        [{"min(fdate)": "2026-01-01", "max(fdate)": "2026-08-19"}],
        [{"COUNT(*)": 42}],
        [{"count(1)": 42}],
        [{"count(*)": 0}],
        # Bare keyword form (psycopg2 unaliased SELECT MIN(x)/COUNT(*)).
        [{"count": 5}],
        [{"min": "2026-01-01", "max": "2026-08-19"}],
        # Whitespace / case tolerance inside the function form.
        [{"MIN ( FDATE )": "2026-01-01", "MAX ( FDATE )": "2026-08-19"}],
        # Two metadata rows are still the metadata-only shape.
        [{"min_fdate": "2026-01-01"}, {"max_fdate": "2026-08-19"}],
        [{"count_rows": 3}, {"count_rows": 7}],
    ],
)
def test_metadata_only_detected(rows) -> None:
    assert is_metadata_only_rows(rows) is True


# ── False: NOT metadata-only shapes ───────────────────────────────────────


@pytest.mark.parametrize(
    "rows",
    [
        # Real business data with a measure column must never be flagged.
        [{"product_name": "Widget", "total_revenue": 100}],
        # Mixed aggregate + measure: NOT every column matches.
        [{"MIN_FDATE": "2026-01-01", "total_revenue": 100}],
        # A single real business column.
        [{"order_date": "2026-01-01"}],
        [{"total_revenue": 100}],
        # AVG / SUM are business aggregates, not metadata.
        [{"avg_price": 42.5}],
        [{"sum(revenue)": 1000}],
        # Too many rows for a metadata snapshot.
        [
            {"min_fdate": "2026-01-01"},
            {"min_fdate": "2026-01-02"},
            {"min_fdate": "2026-01-03"},
        ],
        # Empty / absent / unshaped inputs.
        [],
        None,
        [{}],
        [{"": 1}],
        ["not-a-dict"],
    ],
)
def test_metadata_only_not_detected(rows) -> None:
    assert is_metadata_only_rows(rows) is False


# ── Integration with the empty detector ───────────────────────────────────


def test_metadata_only_is_not_effective_empty() -> None:
    """A metadata-only result carries signal (date strings are non-empty), so
    is_effective_empty must NOT treat it as empty — the contract relies on the
    shape detector separately."""
    rows = [{"MIN_FDATE": "2026-01-01", "MAX_FDATE": "2026-08-19"}]
    assert is_metadata_only_rows(rows) is True


def test_detector_is_case_and_whitespace_insensitive() -> None:
    assert is_metadata_only_rows([{"mIn_Date": "x", "MaX_Date": "y"}]) is True
    assert is_metadata_only_rows([{"  COUNT (*)  ": 1}]) is True
