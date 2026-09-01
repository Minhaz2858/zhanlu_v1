"""Test feedback-driven adjustment (P1-4A).

Validates that compute_feedback_adjustment() and apply_feedback_adjustment()
correctly adjust forecasts based on scored user corrections.
"""
import datetime as _dt
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services.forecasting.ops.feedback_trainer import (
    compute_feedback_adjustment,
    apply_feedback_adjustment,
)


class MockFeedbackRow:
    """Minimal mock for ForecastFeedback row."""
    def __init__(self, ai_price, user_price, ai_error, user_error,
                 target_date=None, scored_at=None, beat=True, status="scored"):
        self.ai_price = ai_price
        self.user_price = user_price
        self.ai_error = ai_error
        self.user_error = user_error
        self.target_date = target_date
        self.scored_at = scored_at
        self.beat = beat
        self.status = status


def _make_db(rows):
    """Build a mock DB with ForecastFeedback rows."""
    db = MagicMock()
    mock_query = MagicMock()
    mock_filter = MagicMock()
    mock_order = MagicMock()
    mock_limit = MagicMock()

    mock_query.filter.return_value = mock_filter
    mock_filter.order_by.return_value = mock_order
    mock_order.limit.return_value = mock_limit
    mock_limit.all.return_value = rows

    db.query.return_value = mock_query
    return db


class TestComputeFeedbackAdjustment:
    """Tests for compute_feedback_adjustment()."""

    def test_no_feedback_returns_zero(self):
        """When no scored feedback exists, adjustment is 0.0."""
        db = _make_db([])
        result = compute_feedback_adjustment(
            db, "t1", [100.0, 101.0, 102.0],
            forecast_date=_dt.datetime(2024, 6, 1),
        )
        assert result == 0.0

    def test_user_perfect_correction(self):
        """When user was perfectly right (user_error=0), adjustment is full correction."""
        rows = [
            MockFeedbackRow(
                ai_price=100.0, user_price=110.0,
                ai_error=10.0, user_error=0.0,
                scored_at=_dt.datetime(2024, 5, 30),
            ),
        ]
        db = _make_db(rows)
        result = compute_feedback_adjustment(
            db, "t1", [100.0] * 7,
            forecast_date=_dt.datetime(2024, 6, 1),
            decay_days=100.0,  # long decay so recency doesn't dampen much
        )
        # Correction = user_price - ai_price = 110 - 100 = +10
        # Quality weight = 1.0 - 0/10 = 1.0
        # Result should be close to +10 but dampened to max 10% of forecast = 10
        assert result > 5.0
        assert result <= 10.0

    def test_user_barely_better(self):
        """When user barely beat AI, weight is low → small adjustment."""
        rows = [
            MockFeedbackRow(
                ai_price=100.0, user_price=101.0,
                ai_error=10.0, user_error=9.0,
                scored_at=_dt.datetime(2024, 5, 30),
            ),
        ]
        db = _make_db(rows)
        result = compute_feedback_adjustment(
            db, "t1", [100.0] * 7,
            forecast_date=_dt.datetime(2024, 6, 1),
            decay_days=100.0,
        )
        # Correction = 1.0, quality weight = 1 - 9/10 = 0.1 (clamped)
        # Total weight = 0.1 → very small adjustment
        assert abs(result) < 2.0

    @pytest.mark.skip(reason="Mock query chain doesn't properly filter by scored_at — tested in integration")
    def test_recency_decay(self):
        """Older feedback should have less influence due to recency decay."""
        # This test requires a real DB to properly test recency decay
        # because the mock query chain doesn't filter/order by scored_at.
        pass

    def test_multiple_feedback_averaged(self):
        """Multiple feedback rows should be weighted-averaged."""
        rows = [
            MockFeedbackRow(
                ai_price=100.0, user_price=110.0,  # +10 correction
                ai_error=10.0, user_error=0.0,
                scored_at=_dt.datetime(2024, 5, 30),
            ),
            MockFeedbackRow(
                ai_price=100.0, user_price=105.0,  # +5 correction
                ai_error=10.0, user_error=2.0,
                scored_at=_dt.datetime(2024, 5, 29),
            ),
        ]
        db = _make_db(rows)
        result = compute_feedback_adjustment(
            db, "t1", [100.0] * 7,
            forecast_date=_dt.datetime(2024, 6, 1),
            decay_days=100.0,
        )
        # Result should be between 5 and 10 (weighted average)
        assert 5.0 < result < 10.0

    @pytest.mark.skip(reason="Mock query chain doesn't filter by status — tested in integration")
    def test_min_confidence_filter(self):
        """Non-scored feedback should be excluded."""
        pass


class TestApplyFeedbackAdjustment:
    """Tests for apply_feedback_adjustment()."""

    def test_zero_adjustment_returns_unchanged(self):
        """When adjustment is 0, forecast should be unchanged."""
        fc = [100.0, 101.0, 102.0]
        result = apply_feedback_adjustment(fc, 0.0)
        assert result == fc

    def test_positive_adjustment(self):
        """Positive adjustment should increase all values."""
        fc = [100.0] * 7
        result = apply_feedback_adjustment(fc, 5.0)
        assert all(r > 100.0 for r in result)
        # First step gets full adjustment, later steps get slightly less
        assert result[0] == 105.0
        assert result[6] < 105.0  # dampened by horizon

    def test_negative_adjustment(self):
        """Negative adjustment should decrease all values."""
        fc = [100.0] * 7
        result = apply_feedback_adjustment(fc, -5.0)
        assert all(r < 100.0 for r in result)
        assert result[0] == 95.0

    def test_horizon_dampening(self):
        """Later steps should receive less adjustment than earlier steps."""
        fc = [100.0] * 7
        result = apply_feedback_adjustment(fc, 10.0)
        # Step 0: 100 + 10 * 1.0 = 110
        # Step 6: 100 + 10 * 0.70 = 107
        assert result[0] == 110.0
        assert result[6] == 107.0
        assert result[0] > result[6]
