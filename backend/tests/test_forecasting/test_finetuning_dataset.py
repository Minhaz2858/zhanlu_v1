"""Tests for pooled petrochem dataset builder (Phase B fine-tuning)."""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.finetuning.dataset_builder import (
    PooledSample,
    build_pooled_dataset,
    split_temporal_holdout,
)


def _make_series(n=100, seed=42):
    rng = np.random.RandomState(seed)
    return pd.Series(
        np.random.randn(n) * 5 + 100,
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )


def test_pooled_sample_fields():
    """PooledSample must have context, target, product_key, exog, dates."""
    s = PooledSample(
        context=np.zeros(60),
        target=np.zeros(7),
        product_key="test",
        exog=None,
        target_start_date=pd.Timestamp("2025-01-01"),
        target_end_date=pd.Timestamp("2025-01-07"),
    )
    assert len(s.context) == 60
    assert len(s.target) == 7
    assert s.product_key == "test"
    assert s.exog is None


def test_build_pooled_dataset_basic():
    """Given multiple pd.Series, build sliding-window samples."""
    series_map = {
        "product_a": _make_series(100, seed=1),
        "product_b": _make_series(80, seed=2),
    }
    samples = build_pooled_dataset(
        series_map, context_len=60, horizon=7, step=7
    )
    assert len(samples) > 0
    assert all(len(s.context) == 60 for s in samples)
    assert all(len(s.target) == 7 for s in samples)
    # Both products should be represented
    products = {s.product_key for s in samples}
    assert "product_a" in products
    assert "product_b" in products


def test_build_pooled_dataset_skips_short_series():
    """Series shorter than context_len + horizon should be skipped."""
    series_map = {
        "short": _make_series(50, seed=1),
        "long": _make_series(100, seed=2),
    }
    samples = build_pooled_dataset(
        series_map, context_len=60, horizon=7, step=7
    )
    products = {s.product_key for s in samples}
    assert "short" not in products
    assert "long" in products


def test_build_pooled_dataset_step_affects_count():
    """Smaller step = more samples (denser sliding window)."""
    y = _make_series(100, seed=1)
    samples_dense = build_pooled_dataset(
        {"p": y}, context_len=60, horizon=7, step=1
    )
    samples_sparse = build_pooled_dataset(
        {"p": y}, context_len=60, horizon=7, step=7
    )
    assert len(samples_dense) > len(samples_sparse)


def test_build_pooled_dataset_with_exog():
    """Optional exog DataFrame is attached to each sample."""
    y = _make_series(100, seed=1)
    exog = pd.DataFrame(
        {"feat": np.random.randn(100) * 10 + 200}, index=y.index
    )
    samples = build_pooled_dataset(
        {"p": y}, context_len=60, horizon=7, step=7, exog_map={"p": exog}
    )
    assert len(samples) > 0
    assert samples[0].exog is not None
    assert samples[0].exog.shape[0] == 60  # context-aligned exog


def test_build_pooled_dataset_without_exog_has_none():
    """Samples without exog_map should have exog=None."""
    y = _make_series(100, seed=1)
    samples = build_pooled_dataset(
        {"p": y}, context_len=60, horizon=7, step=7
    )
    assert all(s.exog is None for s in samples)


def test_temporal_holdout_split():
    """Reserve last N days as strict holdout (never in training)."""
    y = _make_series(100, seed=1)
    samples = build_pooled_dataset(
        {"p": y}, context_len=60, horizon=7, step=7
    )
    train, holdout = split_temporal_holdout(samples, holdout_days=14)
    assert len(holdout) > 0
    assert len(train) > len(holdout)
    # No overlap: holdout targets are all later than train targets
    train_max_date = max(s.target_end_date for s in train)
    holdout_min_date = min(s.target_start_date for s in holdout)
    assert holdout_min_date >= train_max_date


def test_temporal_holdout_empty_samples():
    """Empty sample list should return empty splits."""
    train, holdout = split_temporal_holdout([], holdout_days=14)
    assert train == []
    assert holdout == []


def test_context_and_target_are_contiguous():
    """Context window immediately precedes target window."""
    y = _make_series(100, seed=1)
    samples = build_pooled_dataset(
        {"p": y}, context_len=60, horizon=7, step=7
    )
    for s in samples:
        # The last context value should be the value just before target_start
        ctx_idx = y.index.get_loc(s.target_start_date) - 1
        assert abs(s.context[-1] - y.iloc[ctx_idx]) < 1e-5
