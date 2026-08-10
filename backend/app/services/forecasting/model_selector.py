"""P3-2: Automated model selection — per-product rolling backtest pool pruning.

Allows the engine to dynamically reduce the model pool per product based on
historical backtest performance, cutting models that consistently underperform.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_SKILL_RATIO_THRESHOLD = 0.5
_DEFAULT_LOOKBACK_WINDOW = 30


def select_model_pool(
    base_pool: dict[str, Any],
    product_key: str,
    rolling_mape: dict[str, list[float]] | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_WINDOW,
    skill_ratio_threshold: float = _DEFAULT_SKILL_RATIO_THRESHOLD,
    min_models: int = 3,
) -> dict[str, Any]:
    """Filter the model pool to only models with acceptable historical performance.

    Args:
        base_pool: Full model pool dict.
        product_key: Product identifier.
        rolling_mape: {model_name: [mape values over time]}.
        lookback_days: Rolling window length.
        skill_ratio_threshold: Min ratio vs best to keep model.
        min_models: Always keep at least this many.

    Returns:
        Filtered pool dict.
    """
    if rolling_mape is None or len(rolling_mape) < 2:
        return base_pool

    model_avg_mape: dict[str, float] = {}
    for name, errors in rolling_mape.items():
        if name not in base_pool:
            continue
        window = errors[-lookback_days:] if len(errors) > lookback_days else errors
        if window:
            model_avg_mape[name] = float(np.mean(window))

    if not model_avg_mape:
        return base_pool

    best_mape = min(model_avg_mape.values())
    if best_mape <= 0:
        return base_pool

    threshold_mape = best_mape / skill_ratio_threshold
    selected: dict[str, Any] = {}
    for name, model in base_pool.items():
        if name in model_avg_mape and model_avg_mape[name] > threshold_mape:
            logger.info("[model-selector] %s: pruning %s (MAPE %.2f > %.2f)", product_key, name, model_avg_mape[name], threshold_mape)
            continue
        selected[name] = model

    if len(selected) < min_models:
        pruned = sorted(
            [(n, m) for n, m in base_pool.items() if n not in selected and n in model_avg_mape],
            key=lambda x: model_avg_mape[x[0]],
        )
        for name, model in pruned:
            if len(selected) >= min_models:
                break
            selected[name] = model

    logger.info("[model-selector] %s: %d/%d models kept", product_key, len(selected), len(base_pool))
    return selected
