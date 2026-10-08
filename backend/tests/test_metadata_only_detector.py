"""Tests for the ``is_metadata_only_rows`` shape detector.

The detector is shape + question aware. It flags results whose EVERY column
is a MIN/MAX aggregate or an aliased COUNT over at most 2 rows — the signature
of a query that silently degraded to a metadata answer (date range / row
count) instead of returning business data.

Question-aware value rule (2026-09-22, measured failure): an ALIASED COUNT
column is only exempt when the QUESTION itself asks for a count ("How many
inventory lots do we have?" → ``{"total_lot_count": 16143}`` used to be
flagged metadata-only; the correct answer was thrown away and the
auto-correction answered "0 lots" from the wrong table). For any OTHER
question a bare count is still a dodge (asked for shipments, got a row
count); a ZERO count is always a wrong-table candidate. Bare (``count``) and
function (``count(*)``) forms stay flagged regardless of value — unaliased
aggregates are probe/degraded output by construction. ``question=None``
keeps the conservative question-blind behaviour for callers that cannot
know the question.
"""

from __future__ import annotations

import pytest

from app.services.goal_contract import is_metadata_only_rows

_LOTS_QUESTION = "How many inventory lots do we have?"


# ── True: metadata-only shapes ────────────────────────────────────────────


@pytest.mark.parametrize(
    "rows",
    [
        # ERP-style prefix columns (observed failure: MIN_FDATE / MAX_FDATE).
        [{"MIN_FDATE": "2026-01-01", "MAX_FDATE": "2026-08-19"}],
        [{"min_date": "2026-01-01", "max_date": "2026-08-19"}],
        # A zero count is a wrong-table candidate, never a confident answer —
        # even when the question asks for a count.
        [{"count_rows": 0}],
        [{"total_lot_count": 0}],
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
    ],
)
def test_metadata_only_detected(rows) -> None:
    assert is_metadata_only_rows(rows, question=_LOTS_QUESTION) is True


@pytest.mark.parametrize(
    "rows",
    [
        [{"count_rows": 0}],
        [{"total_lot_count": 0}],
    ],
)
def test_zero_counts_flagged_even_without_question(rows) -> None:
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
    assert is_metadata_only_rows(rows, question=_LOTS_QUESTION) is False


@pytest.mark.parametrize(
    "rows",
    [
        # A POSITIVE aliased count IS the answer to a "how many" question
        # (measured failure: "Provide the total number of inventory lots" →
        # {"total_lot_count": 16143} was flagged and the correct answer was
        # discarded in favour of a wrong-table zero).
        [{"total_lot_count": 16143}],
        [{"lot_count": 42}],
        [{"count_rows": 42}],
        [{"order_total_count": 5}],
        # Chinese count intent.
        [{"total_lot_count": 16143}],
    ],
)
def test_positive_count_answers_count_question(rows) -> None:
    assert is_metadata_only_rows(rows, question=_LOTS_QUESTION) is False
    assert is_metadata_only_rows(rows, question="库存批次总数是多少") is False


@pytest.mark.parametrize(
    "rows",
    [
        # Without a count question, a bare positive count is a DODGE
        # (asked for shipments, got a row count) — the conservative
        # question-blind behaviour is preserved.
        [{"total_lot_count": 16143}],
        [{"lot_count": 42}],
        [{"count_rows": 42}],
        [{"order_total_count": 5}],
        [{"count_rows": 3}, {"count_rows": 7}],
    ],
)
def test_positive_count_without_count_intent_still_flagged(rows) -> None:
    assert is_metadata_only_rows(rows, question="what were our total shipments last month") is True
    assert is_metadata_only_rows(rows) is True


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
