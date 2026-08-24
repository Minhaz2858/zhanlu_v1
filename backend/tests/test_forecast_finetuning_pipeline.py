"""Test fine-tuning pipeline: dataset builder + prompt tuning + nightly orchestrator.

Covers:
- build_pooled_dataset() sliding-window extraction
- split_temporal_holdout() temporal splitting
- finetune_runner.run_nightly_finetuning() orchestrator (mocked)
- load_latest_prompt_tokens() from disk
"""
import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.finetuning.dataset_builder import (
    PooledSample,
    build_pooled_dataset,
    split_temporal_holdout,
)


# ---------------------------------------------------------------------------
# Dataset builder
# ---------------------------------------------------------------------------

class TestBuildPooledDataset:
    """Tests for build_pooled_dataset()."""

    def test_basic_sliding_window(self):
        """With enough data, should produce samples."""
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        series_map = {
            "naphtha": pd.Series(np.random.uniform(80, 120, 100), index=dates),
            "c5_cracked": pd.Series(np.random.uniform(100, 140, 100), index=dates),
        }
        samples = build_pooled_dataset(series_map, context_len=30, horizon=7, step=7)
        assert len(samples) > 0
        assert all(isinstance(s, PooledSample) for s in samples)
        assert all(len(s.context) == 30 for s in samples)
        assert all(len(s.target) == 7 for s in samples)

    def test_too_short_series_skipped(self):
        """Series shorter than context_len + horizon should be skipped."""
        short_series = {
            "short": pd.Series([1, 2, 3]),  # only 3 points
        }
        samples = build_pooled_dataset(short_series, context_len=30, horizon=7)
        assert len(samples) == 0

    def test_multiple_products(self):
        """Samples from multiple products should all be included."""
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        series_map = {
            "prod_a": pd.Series(np.random.uniform(50, 100, 100), index=dates),
            "prod_b": pd.Series(np.random.uniform(100, 200, 100), index=dates),
            "prod_c": pd.Series(np.random.uniform(200, 300, 100), index=dates),
        }
        samples = build_pooled_dataset(series_map, context_len=30, horizon=7, step=7)
        product_keys = {s.product_key for s in samples}
        assert product_keys == {"prod_a", "prod_b", "prod_c"}

    def test_nan_values_handled(self):
        """NaN values should be dropped before windowing."""
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        vals = np.random.uniform(80, 120, 100)
        vals[50] = np.nan
        series_map = {"naphtha": pd.Series(vals, index=dates)}
        samples = build_pooled_dataset(series_map, context_len=30, horizon=7, step=7)
        # Should still produce samples (NaN dropped)
        assert len(samples) > 0

    def test_exog_features(self):
        """Exogenous features should be included when provided."""
        dates = pd.date_range("2024-01-01", periods=100, freq="D")
        series_map = {
            "naphtha": pd.Series(np.random.uniform(80, 120, 100), index=dates),
        }
        exog = pd.DataFrame(
            {"brent": np.random.uniform(70, 90, 100)},
            index=dates,
        )
        exog_map = {"naphtha": exog}
        samples = build_pooled_dataset(
            series_map, context_len=30, horizon=7, step=7, exog_map=exog_map,
        )
        assert len(samples) > 0
        assert samples[0].exog is not None
        assert samples[0].exog.shape[0] == 30  # context_len
        assert samples[0].exog.shape[1] == 1  # 1 exog feature


class TestSplitTemporalHoldout:
    """Tests for split_temporal_holdout()."""

    def _make_samples(self, n=50):
        """Create synthetic samples with dates spanning 60 days."""
        samples = []
        base_date = _dt.datetime(2024, 1, 1)
        for i in range(n):
            samples.append(PooledSample(
                context=np.zeros(30, dtype=np.float32),
                target=np.zeros(7, dtype=np.float32),
                product_key="test",
                exog=None,
                target_start_date=base_date + _dt.timedelta(days=i),
                target_end_date=base_date + _dt.timedelta(days=i + 6),
            ))
        return samples

    def test_holdout_split(self):
        """Last 14 days should be holdout."""
        samples = self._make_samples(50)
        train, holdout = split_temporal_holdout(samples, holdout_days=14)
        assert len(train) + len(holdout) == len(samples)
        assert len(holdout) > 0
        # Holdout should contain the most recent samples
        max_train_date = max(s.target_start_date for s in train)
        min_holdout_date = min(s.target_start_date for s in holdout)
        assert min_holdout_date > max_train_date

    def test_empty_samples(self):
        """Empty list should return (empty, empty)."""
        train, holdout = split_temporal_holdout([])
        assert train == []
        assert holdout == []

    def test_no_look_ahead_bias(self):
        """No sample should appear in both train and holdout."""
        samples = self._make_samples(50)
        train, holdout = split_temporal_holdout(samples, holdout_days=14)
        train_dates = {s.target_start_date for s in train}
        holdout_dates = {s.target_start_date for s in holdout}
        assert train_dates.isdisjoint(holdout_dates)


# ---------------------------------------------------------------------------
# Finetune runner (orchestrator)
# ---------------------------------------------------------------------------

class TestFinetuneRunner:
    """Tests for run_nightly_finetuning() orchestrator."""

    def test_disabled_returns_status(self):
        """When FORECAST_FINETUNING_ENABLED=false, returns disabled status."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.FORECAST_FINETUNING_ENABLED = False
            from app.services.forecasting.finetuning.finetune_runner import run_nightly_finetuning
            result = run_nightly_finetuning(MagicMock())
            assert result["status"] == "disabled"

    def test_no_series_returns_error(self):
        """When no price series can be loaded, returns error."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.FORECAST_FINETUNING_ENABLED = True
            mock_settings.FORECAST_FINETUNING_PROMPT_TOKENS = 16
            mock_settings.FORECAST_FINETUNING_LR = 1e-3
            mock_settings.FORECAST_FINETUNING_EPOCHS = 5

            db = MagicMock()
            db.query.return_value.filter.return_value.all.return_value = []

            from app.services.forecasting.finetuning.finetune_runner import run_nightly_finetuning
            result = run_nightly_finetuning(db)
            assert "no_price_series" in result.get("errors", [])


class TestLoadLatestPromptTokens:
    """Tests for load_latest_prompt_tokens()."""

    def test_loads_from_disk(self):
        """Should load the latest prompt tokens from the output directory."""
        from app.services.forecasting.finetuning.finetune_runner import load_latest_prompt_tokens

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake prompt_tokens_latest.npy
            fake_tokens = np.random.randn(16, 256).astype(np.float32)
            latest_path = Path(tmpdir) / "prompt_tokens_latest.npy"
            np.save(str(latest_path), fake_tokens)

            loaded = load_latest_prompt_tokens(tmpdir)
            assert loaded is not None
            assert loaded.shape == (16, 256)
            np.testing.assert_array_almost_equal(loaded, fake_tokens)

    def test_returns_none_when_no_tokens(self):
        """Should return None when no tokens file exists."""
        from app.services.forecasting.finetuning.finetune_runner import load_latest_prompt_tokens

        with tempfile.TemporaryDirectory() as tmpdir:
            loaded = load_latest_prompt_tokens(tmpdir)
            assert loaded is None
