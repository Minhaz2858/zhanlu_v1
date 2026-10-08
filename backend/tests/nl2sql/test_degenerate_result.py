"""Zero-row correction acceptance gate (2026-09-22, measured failure).

"Provide the total number of inventory lots": the correct COUNT on
``erp_t_bd_lotmaster`` (16,143 rows live) was discarded as metadata, the
re-selection moved to ``erp_t_stk_inventory`` where ``FMTONO`` is NULL on all
52,647 rows, and ``{"total_inventory_lots": 0}`` was accepted as "correction
SUCCEEDED". The narration then asserted "No inventory lots are currently
recorded".

Pins:
- a positive count answers a "how many" question — never degenerate;
- an all-null / all-zero corrected result is NOT a successful correction —
  it is the same empty signal the correction was trying to escape, and
  adopting it replaces a failed attempt with a confidently wrong answer.
"""

from __future__ import annotations

from app.services.db.nl_answer_service import _is_degenerate_result

_LOTS_QUESTION = "How many inventory lots do we have?"


def test_positive_count_is_not_degenerate():
    """The measured false answer: a real COUNT result must never be flagged."""
    assert _is_degenerate_result([{"total_lot_count": 16143}], _LOTS_QUESTION) is False
    assert _is_degenerate_result([{"lot_count": 42}], _LOTS_QUESTION) is False


def test_positive_count_question_blind_stays_conservative():
    """Without the question the detector keeps the dodge protection."""
    assert _is_degenerate_result([{"total_lot_count": 16143}]) is True


def test_all_zero_count_is_degenerate():
    assert _is_degenerate_result([{"total_inventory_lots": 0}], _LOTS_QUESTION) is True


def test_all_null_rows_are_degenerate():
    assert _is_degenerate_result([{"a": None, "b": ""}], _LOTS_QUESTION) is True


def test_zero_and_positive_mix_is_not_degenerate():
    assert _is_degenerate_result([{"qty": 0, "amount": 10}], _LOTS_QUESTION) is False
