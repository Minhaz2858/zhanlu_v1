"""P2-2: Backtest significance testing — Diebold-Mariano, bootstrap CI, weighted skill.

Pure-function utilities that consume BacktestResult or raw residual lists.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class SignificanceResult:
    model_a: str
    model_b: str
    dm_statistic: float
    p_value: float
    significant: bool       # p_value < 0.05
    better_model: str       # "model_a", "model_b", or "tie"


def diebold_mariano_test(
    residuals_a: list[float],
    residuals_b: list[float],
    horizon: int = 1,
    alternative: str = "two-sided",
) -> SignificanceResult:
    """Diebold-Mariano test for equal predictive accuracy.

    Tests whether model_a's squared errors are significantly different from
    model_b's using a standard DM test with Newey-West HAC variance.

    Args:
        residuals_a: Forecast errors from model A.
        residuals_b: Forecast errors from model B.
        horizon: Forecast horizon for HAC lag truncation.
        alternative: "two-sided" | "greater" (a worse) | "less" (a better).

    Returns:
        SignificanceResult with test statistic, p-value, and interpretation.
    """
    e_a = np.asarray(residuals_a, dtype=float)
    e_b = np.asarray(residuals_b, dtype=float)
    n = len(e_a)

    if n < 5 or len(e_b) < 5:
        return SignificanceResult(
            model_a="model_a", model_b="model_b",
            dm_statistic=0.0, p_value=1.0, significant=False, better_model="tie",
        )

    # Loss differential: d_t = e_a^2 - e_b^2
    d = e_a ** 2 - e_b ** 2
    d_mean = np.mean(d)

    if d_mean == 0:
        return SignificanceResult(
            model_a="model_a", model_b="model_b",
            dm_statistic=0.0, p_value=1.0, significant=False, better_model="tie",
        )

    # Newey-West HAC variance
    hac_var = _newey_west_variance(d, max_lag=horizon)
    if hac_var <= 0:
        return SignificanceResult(
            model_a="model_a", model_b="model_b",
            dm_statistic=0.0, p_value=1.0, significant=False, better_model="tie",
        )

    dm_stat = d_mean / np.sqrt(hac_var / n)

    # Compute p-value from normal distribution
    from scipy.stats import norm
    if alternative == "greater":
        p_value = float(1 - norm.cdf(dm_stat))
    elif alternative == "less":
        p_value = float(norm.cdf(dm_stat))
    else:  # two-sided
        p_value = float(2 * (1 - norm.cdf(abs(dm_stat))))

    significant = p_value < 0.05

    if not significant:
        better = "tie"
    elif d_mean < 0:
        better = "model_a"  # a has smaller squared errors
    else:
        better = "model_b"

    return SignificanceResult(
        model_a="model_a", model_b="model_b",
        dm_statistic=round(float(dm_stat), 4),
        p_value=round(p_value, 4),
        significant=significant,
        better_model=better,
    )


def mape_bootstrap_ci(
    errors: list[float],
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Bootstrap confidence interval for MAPE.

    Args:
        errors: List of absolute percentage errors.
        n_bootstrap: Number of bootstrap samples.
        confidence: Confidence level (default 0.95).

    Returns:
        (lower, upper) MAPE bounds.
    """
    e = np.asarray(errors, dtype=float)
    n = len(e)

    if n < 5:
        m = float(np.nanmean(e) if len(e) > 0 else 0.0)
        return (m, m)

    rng = np.random.RandomState(42)
    boot_means = []
    for _ in range(n_bootstrap):
        sample = rng.choice(e, size=n, replace=True)
        boot_means.append(float(np.mean(sample)))

    alpha = (1.0 - confidence) / 2.0
    lower = float(np.percentile(boot_means, alpha * 100))
    upper = float(np.percentile(boot_means, (1.0 - alpha) * 100))
    return (round(lower, 4), round(upper, 4))


def horizon_weighted_skill(
    per_horizon_mape: dict[int, dict[str, float]],
    naive_model: str = "seasonal_naive",
    weights: dict[int, float] | None = None,
) -> dict[str, float]:
    """Compute horizon-weighted skill score vs a reference model.

    Skill = 1 - (model_mape / reference_mape). Values > 0 indicate improvement
    over reference; < 0 indicate degradation.

    Args:
        per_horizon_mape: {horizon: {model_name: mape}} from BacktestResult.
        naive_model: Name of the reference/naive model.
        weights: Optional {horizon: weight} dict. Defaults to linearly
                 decreasing weights (closer horizons weighted more).

    Returns:
        Dict of model_name -> weighted_skill_score.
    """
    if not per_horizon_mape:
        return {}

    horizons = sorted(per_horizon_mape.keys())

    if weights is None:
        # Default: linear decay — closer horizons weighted more
        max_h = max(horizons) if horizons else 1
        raw = {h: max_h - h + 1 for h in horizons}
        w_sum = sum(raw.values())
        weights = {h: raw[h] / w_sum for h in horizons}

    # Collect all model names
    model_names: set[str] = set()
    for h_data in per_horizon_mape.values():
        model_names.update(h_data.keys())

    skills: dict[str, float] = {}
    for model in model_names:
        weighted_sum = 0.0
        weight_total = 0.0
        for h in horizons:
            m = per_horizon_mape[h]
            if model in m and naive_model in m and m[naive_model] > 0:
                skill_h = 1.0 - (m[model] / m[naive_model])
                w = weights.get(h, 0.0)
                weighted_sum += skill_h * w
                weight_total += w
        if weight_total > 0:
            skills[model] = round(weighted_sum / weight_total, 4)
        else:
            skills[model] = 0.0

    return skills


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _newey_west_variance(x: np.ndarray, max_lag: int = 1) -> float:
    """Newey-West HAC variance estimator."""
    n = len(x)
    x_centered = x - np.mean(x)
    v = np.sum(x_centered ** 2) / n  # Bartlett kernel: lag 0 weight = 1

    for lag in range(1, min(max_lag + 1, n - 1)):
        weight = 1.0 - lag / (max_lag + 1.0)
        autocov = np.sum(x_centered[lag:] * x_centered[:-lag]) / n
        v += 2.0 * weight * autocov

    return max(v, 1e-10)
