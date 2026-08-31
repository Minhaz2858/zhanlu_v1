"""Pooled dataset builder for cross-product fine-tuning.

Collects all available price series (lz_v_* views + ERP tables), applies
sliding-window context/target extraction, and produces PooledSample objects
suitable for Chronos-Bolt prompt-tuning or Moirai LoRA fine-tuning.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd


@dataclass
class PooledSample:
    """A single training sample for fine-tuning.

    Attributes
    ----------
    context : np.ndarray
        Context window values (context_len,).
    target : np.ndarray
        Target window values (horizon,).
    product_key : str
        Product identifier for cross-product learning.
    exog : np.ndarray | None
        Context-aligned exogenous features (context_len, n_exog) or None.
    target_start_date : datetime
        Start date of the target window (for temporal holdout splitting).
    target_end_date : datetime
        End date of the target window.
    """

    context: np.ndarray
    target: np.ndarray
    product_key: str
    exog: np.ndarray | None
    target_start_date: datetime
    target_end_date: datetime


def build_pooled_dataset(
    series_map: dict[str, pd.Series],
    context_len: int = 60,
    horizon: int = 7,
    step: int = 7,
    exog_map: dict[str, pd.DataFrame] | None = None,
) -> list[PooledSample]:
    """Build sliding-window samples from multiple product series.

    Parameters
    ----------
    series_map : {product_key: pd.Series}
        Daily price series for each product.
    context_len : int
        Number of context points (history window). Default 60.
    horizon : int
        Forecast horizon (target window length). Default 7.
    step : int
        Sliding-window step size. Default 7.
    exog_map : {product_key: pd.DataFrame} | None
        Optional exogenous features aligned to each series.

    Returns
    -------
    list[PooledSample]
        Samples from all products, ordered by product then by time.
    """
    samples: list[PooledSample] = []
    exog_map = exog_map or {}

    for product_key, y in series_map.items():
        y = y.dropna()
        if len(y) < context_len + horizon:
            continue
        exog = exog_map.get(product_key)

        for start in range(0, len(y) - context_len - horizon + 1, step):
            ctx = y.iloc[start : start + context_len].values.astype(np.float32)
            tgt = y.iloc[start + context_len : start + context_len + horizon].values.astype(
                np.float32
            )
            exog_slice = None
            if exog is not None:
                exog_aligned = exog.reindex(
                    y.index[start : start + context_len]
                ).ffill().fillna(0.0)
                exog_slice = exog_aligned.values.astype(np.float32)

            tgt_start = y.index[start + context_len]
            tgt_end = y.index[start + context_len + horizon - 1]
            samples.append(
                PooledSample(
                    context=ctx,
                    target=tgt,
                    product_key=product_key,
                    exog=exog_slice,
                    target_start_date=(
                        tgt_start.to_pydatetime()
                        if hasattr(tgt_start, "to_pydatetime")
                        else tgt_start
                    ),
                    target_end_date=(
                        tgt_end.to_pydatetime()
                        if hasattr(tgt_end, "to_pydatetime")
                        else tgt_end
                    ),
                )
            )
    return samples


def split_temporal_holdout(
    samples: list[PooledSample], holdout_days: int = 14
) -> tuple[list[PooledSample], list[PooledSample]]:
    """Split samples into train + strict temporal holdout.

    Holdout = samples whose target_start_date is in the last
    ``holdout_days`` of the overall date range. No sample in holdout
    appears in train — this prevents look-ahead bias.

    Parameters
    ----------
    samples : list[PooledSample]
    holdout_days : int
        Number of days from the end to reserve as holdout.

    Returns
    -------
    (train, holdout) : tuple of lists
    """
    if not samples:
        return [], []
    all_dates = [s.target_start_date for s in samples]
    cutoff = max(all_dates) - pd.Timedelta(days=holdout_days)
    train = [s for s in samples if s.target_start_date <= cutoff]
    holdout = [s for s in samples if s.target_start_date > cutoff]
    return train, holdout
