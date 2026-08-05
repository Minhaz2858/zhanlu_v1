"""Tests for Chronos-Bolt prompt-tuning harness (Phase B, CPU)."""
import numpy as np
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from app.services.forecasting.finetuning.dataset_builder import PooledSample


def _make_samples(n=10, context_len=60, horizon=7):
    return [
        PooledSample(
            context=np.random.randn(context_len).astype(np.float32),
            target=np.random.randn(horizon).astype(np.float32),
            product_key="test",
            exog=None,
            target_start_date=pd.Timestamp(f"2025-01-{i+1:02d}"),
            target_end_date=pd.Timestamp(f"2025-01-{i+8:02d}"),
        )
        for i in range(n)
    ]


def test_prompt_tuning_imports():
    """PromptTuningConfig and train_soft_prompts should be importable."""
    try:
        from app.services.forecasting.finetuning.prompt_tuning import (
            PromptTuningConfig,
            train_soft_prompts,
        )
    except ImportError:
        pytest.skip("torch not installed")
    assert callable(train_soft_prompts)


def test_prompt_tuning_config_defaults():
    """PromptTuningConfig should have sensible defaults."""
    from app.services.forecasting.finetuning.prompt_tuning import (
        PromptTuningConfig,
    )

    cfg = PromptTuningConfig(n_prompt_tokens=16, lr=1e-3, epochs=5, batch_size=4)
    assert cfg.n_prompt_tokens == 16
    assert cfg.lr == 1e-3
    assert cfg.epochs == 5
    assert cfg.batch_size == 4
    assert cfg.context_len == 60
    assert cfg.horizon == 7


def test_train_soft_prompts_mocked():
    """Training loop runs and returns prompt_tokens + loss_history."""
    from app.services.forecasting.finetuning.prompt_tuning import (
        PromptTuningConfig,
        train_soft_prompts,
    )

    cfg = PromptTuningConfig(
        n_prompt_tokens=4, lr=1e-3, epochs=2, batch_size=2
    )
    samples = _make_samples(10)

    # Mock the chronos model loading
    mock_model = MagicMock()
    mock_pipeline = MagicMock()
    # Mock predict to return a tensor-like object
    fake_forecast = np.random.randn(1, 20, 7).astype(np.float32) * 10 + 100
    mock_pipeline.predict.return_value = MagicMock(
        __getitem__=lambda s, k: fake_forecast[k],
        numpy=lambda: fake_forecast,
        median=lambda axis=0: np.median(fake_forecast, axis=axis),
        dim=lambda: 3,
    )

    with patch(
        "app.services.forecasting.finetuning.prompt_tuning._load_chronos_model",
        return_value=(mock_model, mock_pipeline),
    ):
        result = train_soft_prompts(samples, cfg)

    assert "prompt_tokens" in result
    assert "loss_history" in result
    assert len(result["loss_history"]) > 0
    assert result["prompt_tokens"].shape[0] == cfg.n_prompt_tokens


def test_train_soft_prompts_loss_history_length():
    """Loss history should have one entry per epoch."""
    from app.services.forecasting.finetuning.prompt_tuning import (
        PromptTuningConfig,
        train_soft_prompts,
    )

    cfg = PromptTuningConfig(
        n_prompt_tokens=4, lr=1e-3, epochs=5, batch_size=2
    )
    samples = _make_samples(10)

    mock_model = MagicMock()
    mock_pipeline = MagicMock()
    fake_forecast = np.random.randn(1, 20, 7).astype(np.float32)
    mock_pipeline.predict.return_value = MagicMock(
        numpy=lambda: fake_forecast,
        dim=lambda: 3,
    )

    with patch(
        "app.services.forecasting.finetuning.prompt_tuning._load_chronos_model",
        return_value=(mock_model, mock_pipeline),
    ):
        result = train_soft_prompts(samples, cfg)

    assert len(result["loss_history"]) == cfg.epochs


def test_train_soft_prompts_empty_samples():
    """Empty samples should not crash — return zero-length history."""
    from app.services.forecasting.finetuning.prompt_tuning import (
        PromptTuningConfig,
        train_soft_prompts,
    )

    cfg = PromptTuningConfig(
        n_prompt_tokens=4, lr=1e-3, epochs=3, batch_size=2
    )

    mock_model = MagicMock()
    mock_pipeline = MagicMock()

    with patch(
        "app.services.forecasting.finetuning.prompt_tuning._load_chronos_model",
        return_value=(mock_model, mock_pipeline),
    ):
        result = train_soft_prompts([], cfg)

    assert "prompt_tokens" in result
    assert len(result["loss_history"]) == 0
