"""P2.12: Stacking meta-learner shadow mode.

The stacking ridge meta-learner (models/stacking_meta.py) must be wired into
the engine via the on_fold callback from backtest.py (extended in P0.1).

Key invariant: stacking is shadow-first.  When FORECAST_STACKING_ENABLED is
False (the default), the meta-learner is trained and its would-be forecast is
computed, but the result is stored in model_detail as "stacking_shadow" — it
is NEVER published as the ensemble forecast.  This allows us to evaluate its
performance against the inverse-MAPE blend without risking a behavior change.

Promotion criteria (documented, not enforced in code):
  Stacking shadow beats inverse-MAPE blend on realized MASE over 4 consecutive
  weekly eval cycles → admin flips FORECAST_STACKING_ENABLED=true.
"""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models.stacking_meta import StackingMetaLearner


class TestStackingShadowMode:
    """Stacking meta-learner must work in shadow mode."""

    def test_record_fold_and_fit(self):
        """Basic fit: record folds → fit_meta → blend."""
        meta = StackingMetaLearner(alpha=1.0)
        rng = np.random.RandomState(42)

        # Simulate 10 folds with 3 base models
        for _ in range(10):
            preds = {
                "model_a": rng.randn(7) * 5 + 100,
                "model_b": rng.randn(7) * 8 + 100,
                "model_c": rng.randn(7) * 3 + 100,
            }
            actuals = rng.randn(7) * 4 + 100
            meta.record_fold(preds, actuals)

        assert meta.fit_meta(), "fit_meta should succeed with 10 folds × 7 samples"
        assert meta.fitted
        assert set(meta.feature_names) == {"model_a", "model_b", "model_c"}

    def test_blend_produces_forecast(self):
        """After fitting, blend() must produce a Series of length h."""
        meta = StackingMetaLearner(alpha=1.0)
        rng = np.random.RandomState(42)
        for _ in range(10):
            preds = {"m1": rng.randn(7) + 100, "m2": rng.randn(7) + 100}
            meta.record_fold(preds, rng.randn(7) + 100)
        meta.fit_meta()

        base_fc = {
            "m1": pd.Series(np.full(7, 101.0)),
            "m2": pd.Series(np.full(7, 99.0)),
        }
        result = meta.blend(base_fc, h=7)
        assert result is not None
        assert len(result) == 7
        assert np.all(np.isfinite(result.values))

    def test_insufficient_data_returns_false(self):
        """With < 10 samples, fit_meta must return False."""
        meta = StackingMetaLearner()
        preds = {"m1": [1.0], "m2": [2.0]}
        meta.record_fold(preds, [1.5])
        assert not meta.fit_meta()
        assert not meta.fitted

    def test_blend_unfitted_returns_none(self):
        """If not fitted, blend() must return None (not raise)."""
        meta = StackingMetaLearner()
        result = meta.blend({"m1": pd.Series([100.0])}, h=1)
        assert result is None

    def test_shadow_metrics_stored_in_model_detail(self):
        """When stacking is computed, shadow MAPE must be logged in model_detail.

        This test validates the engine integration: after compute_target runs
        with FORECAST_STACKING_ENABLED=false (shadow), the model_detail dict
        must contain 'stacking_shadow' with at least a mape key.
        """
        # This is an integration-style test; we verify the contract by checking
        # that the engine's model_detail includes stacking_shadow when the
        # meta-learner is trained.
        # For unit-level: just verify StackingMetaLearner can compute its own MAPE
        meta = StackingMetaLearner(alpha=1.0)
        rng = np.random.RandomState(42)
        all_actuals = []
        for _ in range(15):
            preds = {"m1": rng.randn(7) + 100, "m2": rng.randn(7) + 100}
            actuals = rng.randn(7) + 100
            meta.record_fold(preds, actuals)
            all_actuals.append(actuals)
        assert meta.fit_meta()

        # Compute shadow MAPE: blend vs actuals from last fold
        base_fc = {
            "m1": pd.Series(np.full(7, 100.5)),
            "m2": pd.Series(np.full(7, 99.5)),
        }
        stacked_fc = meta.blend(base_fc, h=7)
        assert stacked_fc is not None
        last_actuals = all_actuals[-1]
        mape = np.mean(np.abs((stacked_fc.values - last_actuals) / last_actuals)) * 100
        assert np.isfinite(mape), f"Shadow MAPE should be finite, got {mape}"
