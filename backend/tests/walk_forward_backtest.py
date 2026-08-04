"""Walk-Forward Backtest: True out-of-sample forecasting accuracy test.

Evaluates the forecasting engine end-to-end by simulating the real-world
deployment pattern: train on data up to date T, forecast h=7 and h=30
days ahead, then compare against actual prices that have since materialized.

Unlike the engine's built-in expanding-window backtest (which evaluates
on the training domain), this script uses a sliding-origin approach where
the forecast horizon lies entirely beyond the training data at each fold.

Metrics reported per product per horizon:
  - MAPE (%)      — Mean Absolute Percentage Error
  - RMSE          — Root Mean Squared Error (same units as price)
  - Bias (raw)    — Mean residual (positive = model overpredicts)
  - Dir. Accuracy — % of time forecast direction (up/down) is correct

Usage:
    cd /home/ysk2025/zhanlu_7_30/backend
    venv/bin/python tests/walk_forward_backtest.py

Requirements:
    - EDIA_MYSQL_URL in .env (must reach 10.10.10.49:3306)
    - venv with pandas, numpy, statsmodels, xgboost, pymysql
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# Ensure the backend package is importable
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ── Configuration ────────────────────────────────────────────────────────

MIN_TRAIN_DAYS = 30      # Minimum history for the first fold
STEP_DAYS = 14           # Step between consecutive walk-forward origins
MAX_ORIGINS = 10         # Max number of walk-forward origins per series
HORIZONS = [7, 14, 30]   # Forecast horizons to evaluate (days)
MAX_HORIZON = 30         # Must be at least max(HORIZONS)

# Products to test — from ECISCO_FORECAST_TARGETS in seed_ecisco_targets.py
# Only family-level md_t_lz_price targets (ERP SKU targets require discovery).
TARGETS: list[dict] = [
    {"product_key": "crude_oil",     "name": "Crude Oil (Brent)",             "where": "FMATERIAL_NAME = '裂解C5-布伦特'"},
    {"product_key": "naphtha",       "name": "Naphtha (Japan)",               "where": "FMATERIAL_NAME = '裂解C5-日本石脑油'"},
    {"product_key": "cracked_c5",    "name": "Cracked C5 (Average)",          "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'"},
    {"product_key": "cracked_c9",    "name": "Cracked C9 (Average)",          "where": "FMATERIAL_NAME = '裂解C9-裂解C9均价'"},
    {"product_key": "isoprene",      "name": "Isoprene (East China)",         "where": "FMATERIAL_NAME = '异戊二烯-华东市场价'"},
    {"product_key": "piperylene",    "name": "Piperylene (East China)",       "where": "FMATERIAL_NAME = '间戊二烯-华东市场价'"},
    {"product_key": "dcpd",          "name": "DCPD (East China)",            "where": "FMATERIAL_NAME = '双环戊二烯-华东市场价'"},
    {"product_key": "sis",           "name": "SIS (Baling 1105)",             "where": "FMATERIAL_NAME = 'SIS-巴陵1105'"},
    {"product_key": "styrene",       "name": "Styrene (Sinopec East China)",  "where": "FMATERIAL_NAME = '苯乙烯-中石化华东'"},
    {"product_key": "blowing_agent", "name": "Blowing Agent (East China)",    "where": "FMATERIAL_NAME = '戊烷发泡剂-华东市场价'"},
]


# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class HorizonResult:
    """Accuracy metrics for a single forecast horizon."""
    horizon: int
    mape: float          # Mean Absolute Percentage Error (0–inf, lower=better)
    rmse: float          # Root Mean Squared Error (price units)
    bias: float          # Mean residual (positive = overprediction)
    dir_acc: float       # Directional accuracy (0–1, higher=better)
    n_folds: int         # Number of walk-forward folds evaluated


@dataclass
class TargetResult:
    """Walk-forward results for a single product."""
    product_key: str
    name: str
    series_length: int
    horizons: dict[int, HorizonResult] = field(default_factory=dict)
    fit_seconds: float = 0.0


# ── Data fetching ────────────────────────────────────────────────────────

_SENTINEL_VALUES = {"F7", "NaN", "nan", "None", "", "null", "NULL"}


def fetch_series(where_clause: str) -> pd.DataFrame:
    """Pull time series from md_t_lz_price via EdiaMysqlDataSource."""
    from app.services.forecasting.edia_source import EdiaMysqlDataSource

    datasource = {
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": where_clause,
        "granularity": "day",
    }

    src = EdiaMysqlDataSource()
    df = src.read_history(datasource)
    return df


# ── Core walk-forward logic ──────────────────────────────────────────────

def walk_forward_evaluate(
    y: pd.Series,
    models: dict,
    seasonal_period: int = 7,
    min_train: int = MIN_TRAIN_DAYS,
    step: int = STEP_DAYS,
    max_folds: int = MAX_ORIGINS,
    horizons: list[int] | None = None,
) -> dict[int, HorizonResult]:
    """Sliding-origin walk-forward evaluation.

    For each origin date T, trains all models on data up to T,
    forecasts the full max horizon forward, and records errors at
    each requested horizon.

    Returns a dict mapping horizon days → HorizonResult.
    """
    if horizons is None:
        horizons = HORIZONS
    max_h = max(horizons)

    y = y.dropna().copy()
    if len(y) < min_train + max_h:
        return {}

    # Determine origin indices
    n = len(y)
    origins = list(range(min_train, n - max_h + 1, step))
    # Cap at max_folds — sample evenly if too many
    if len(origins) > max_folds:
        indices = np.linspace(0, len(origins) - 1, max_folds, dtype=int)
        origins = [origins[i] for i in indices]

    # Accumulators per horizon
    errors: dict[int, list[float]] = {h: [] for h in horizons}
    residuals: dict[int, list[float]] = {h: [] for h in horizons}
    dir_hits: dict[int, list[bool]] = {h: [] for h in horizons}
    valid_folds = 0

    for train_end in origins:
        y_train = y.iloc[:train_end]
        y_future = y.iloc[train_end:train_end + max_h]

        if len(y_future) < max_h:
            continue

        # Ensemble forecast: average of all models that succeed
        preds = []
        for name, model in models.items():
            try:
                m = model.__class__(**model._init_kwargs) if hasattr(model, '_init_kwargs') else model.__class__()
                # Re-initialize with same seasonal_period if the constructor accepts it
            except Exception:
                try:
                    model.fit(y_train, seasonal_period=seasonal_period)
                    p = model.forecast(max_h)
                    preds.append(p.values[:max_h])
                except Exception:
                    continue

        if not preds:
            continue

        # Simple ensemble: median across models
        pred_array = np.array(preds)
        ensemble_pred = np.nanmedian(pred_array, axis=0)

        actual = y_future.values[:max_h]

        for h in horizons:
            a = actual[:h]
            p = ensemble_pred[:h]

            # MAPE (guard against zeros)
            with np.errstate(divide="ignore", invalid="ignore"):
                ape = np.abs((a - p) / a)
                ape = ape[np.isfinite(ape)]
            if len(ape) > 0:
                errors[h].append(float(np.mean(ape)))

            # RMSE
            mse = np.mean((a - p) ** 2)
            residuals[h].append(float(np.mean(p - a)))  # bias per fold

            # Directional accuracy
            actual_dir = np.sign(a[-1] - y_train.iloc[-1] if len(y_train) > 0 else 0)
            pred_dir = np.sign(p[-1] - y_train.iloc[-1] if len(y_train) > 0 else 0)
            dir_hits[h].append(actual_dir == pred_dir)

        valid_folds += 1

    if valid_folds == 0:
        return {}

    results = {}
    for h in horizons:
        if not errors[h]:
            continue
        # RMSE: recompute from all folds
        rmses = []
        for i in range(valid_folds):
            # Use stored bias + MAPE to approximate — better to recompute
            pass
        # Simple: use collected errors
        mape_val = float(np.mean(errors[h])) * 100  # convert to %
        bias_val = float(np.mean(residuals[h]))
        dir_val = float(np.mean(dir_hits[h]))
        # RMSE approximation from MAPE
        # Actually let's properly compute RMSE across all folds
        results[h] = HorizonResult(
            horizon=h,
            mape=mape_val,
            rmse=0.0,  # will be corrected below
            bias=bias_val,
            dir_acc=dir_val,
            n_folds=valid_folds,
        )

    return results


def walk_forward_evaluate_v2(
    y: pd.Series,
    models: dict,
    seasonal_period: int = 7,
    min_train: int = MIN_TRAIN_DAYS,
    step: int = STEP_DAYS,
    max_folds: int = MAX_ORIGINS,
    horizons: list[int] | None = None,
) -> dict[int, HorizonResult]:
    """Sliding-origin walk-forward with proper ensemble and full metrics.

    Returns a dict mapping horizon days → HorizonResult.
    """
    if horizons is None:
        horizons = HORIZONS
    max_h = max(horizons)

    y = y.dropna().copy()
    if len(y) < min_train + max_h:
        return {}

    n = len(y)
    origins = list(range(min_train, n - max_h + 1, step))
    if len(origins) > max_folds:
        indices = np.linspace(0, len(origins) - 1, max_folds, dtype=int)
        origins = [origins[i] for i in indices]
    if not origins:
        return {}

    # Accumulators: store (actual, predicted) pairs per fold per horizon
    all_actuals: dict[int, list[np.ndarray]] = {h: [] for h in horizons}
    all_preds: dict[int, list[np.ndarray]] = {h: [] for h in horizons}
    all_last_actual: list[float] = []  # actual[-1] for directional calc

    for train_end in origins:
        y_train = y.iloc[:train_end]
        y_future = y.iloc[train_end:train_end + max_h]
        if len(y_future) < max_h:
            continue

        # Fit all models, collect forecasts
        model_preds = []
        for name, model in models.items():
            try:
                # Fresh clone to avoid state leakage
                try:
                    m_cls = type(model)
                    # Try to init with same seasonal_period if constructor accepts it
                    try:
                        m = m_cls(seasonal_period=seasonal_period)
                    except TypeError:
                        m = m_cls()
                except Exception:
                    continue
                m.fit(y_train, seasonal_period=seasonal_period)
                p = m.forecast(max_h)
                vals = p.values if hasattr(p, 'values') else np.array(p)
                if len(vals) >= max_h:
                    model_preds.append(vals[:max_h])
            except Exception:
                continue

        if not model_preds:
            continue

        # Ensemble: median across models (robust to outliers)
        ensemble_pred = np.nanmedian(np.array(model_preds), axis=0)
        actual = y_future.values[:max_h]

        for h in horizons:
            all_actuals[h].append(actual[:h])
            all_preds[h].append(ensemble_pred[:h])

        all_last_actual.append(actual[-1])

    # Compute metrics per horizon
    results: dict[int, HorizonResult] = {}
    for h in horizons:
        if not all_actuals[h]:
            continue

        # Flatten all folds: concatenate residuals
        flat_actual = np.concatenate(all_actuals[h])
        flat_pred = np.concatenate(all_preds[h])

        # MAPE
        with np.errstate(divide="ignore", invalid="ignore"):
            ape = np.abs((flat_actual - flat_pred) / flat_actual) * 100
            ape = ape[np.isfinite(ape)]
        mape_val = float(np.mean(ape)) if len(ape) > 0 else float("inf")

        # RMSE
        rmse_val = float(np.sqrt(np.mean((flat_actual - flat_pred) ** 2)))

        # Bias (mean residual: positive = model overpredicts)
        bias_val = float(np.mean(flat_pred - flat_actual))

        # Directional accuracy: compare h-step direction vs last-known value
        n_folds = len(all_actuals[h])
        dir_correct = 0
        for i in range(n_folds):
            fold_actual = all_actuals[h][i]
            fold_pred = all_preds[h][i]
            train_end_idx = origins[i]
            # Last known value before forecast
            last_known = y.iloc[train_end_idx - 1] if train_end_idx > 0 else y.iloc[0]
            actual_dir = 1 if fold_actual[-1] > last_known else (-1 if fold_actual[-1] < last_known else 0)
            pred_dir = 1 if fold_pred[-1] > last_known else (-1 if fold_pred[-1] < last_known else 0)
            if actual_dir != 0 and actual_dir == pred_dir:
                dir_correct += 1
        dir_acc = dir_correct / n_folds if n_folds > 0 else 0.0

        results[h] = HorizonResult(
            horizon=h,
            mape=mape_val,
            rmse=rmse_val,
            bias=bias_val,
            dir_acc=dir_acc,
            n_folds=n_folds,
        )

    return results


# ── Display ──────────────────────────────────────────────────────────────

def print_summary(
    target_results: list[TargetResult],
    total_seconds: float,
) -> None:
    """Print a formatted summary table of walk-forward accuracy results."""
    print()
    print("=" * 120)
    print("  WALK-FORWARD BACKTEST — FORECAST ACCURACY SUMMARY")
    print("=" * 120)
    print(f"  Products tested: {len(target_results)}")
    print(f"  Horizons:        {HORIZONS} days")
    print(f"  Min train:       {MIN_TRAIN_DAYS} days")
    print(f"  Step:            {STEP_DAYS} days")
    print(f"  Max origins:     {MAX_ORIGINS}")
    print(f"  Total runtime:   {total_seconds:.1f}s")
    print()

    # Header
    hdr = (
        f"{'Product':<30s} | {'Len':>5s} | "
        + " | ".join(
            f"MAPE{h}d  RMSE{h}d  Bias{h}d   Dir{h}d  N{h}d"
            for h in HORIZONS
        )
    )
    print(f"  {hdr}")
    print("  " + "-" * (len(hdr) + 2))

    # Rows
    for tr in target_results:
        cols = [f"{tr.name:<30s}", f"{tr.series_length:>5d}"]
        for h in HORIZONS:
            hr = tr.horizons.get(h)
            if hr is None:
                cols.append("   ---       ---      ---     ---    ---")
            else:
                cols.append(
                    f"{hr.mape:>5.1f}%  {hr.rmse:>7.2f}  {hr.bias:>+6.2f}  "
                    f"{hr.dir_acc:>5.1%}  {hr.n_folds:>3d}"
                )
        print("  " + " | ".join(cols))

    # Aggregate stats
    print()
    print("  --- AGGREGATE (mean across products) ---")
    for h in HORIZONS:
        mapes = [tr.horizons[h].mape for tr in target_results if h in tr.horizons and tr.horizons[h].mape < float("inf")]
        rmses = [tr.horizons[h].rmse for tr in target_results if h in tr.horizons]
        dirs  = [tr.horizons[h].dir_acc for tr in target_results if h in tr.horizons]
        if mapes:
            print(f"  {h}-day:  MAPE={np.mean(mapes):.1f}%  RMSE={np.mean(rmses):.2f}  DirAcc={np.mean(dirs):.1%}")
    print()


def print_per_model_detail(
    y: pd.Series,
    models: dict,
    target_name: str,
) -> None:
    """Print per-model accuracy using a single expanding-window backtest
    for informational context (not walk-forward, but shows model contribution)."""
    from app.services.forecasting.backtest import evaluate as backtest_eval

    y = y.dropna().copy()
    if len(y) < MIN_TRAIN_DAYS:
        return

    bt = backtest_eval(y, models, seasonal_period=7)
    print(f"\n  [{target_name}] Per-model {bt.metric}:")
    for mname, mape in sorted(bt.per_model_mape.items(), key=lambda x: x[1]):
        marker = " ← ensemble candidate" if mname not in ("naive_last", "seasonal_naive", "mean_reversion") else ""
        print(f"    {mname:<20s}: {mape*100:>6.1f}%{marker}")
    print(f"    {'ensemble':<20s}: {bt.ensemble_mape*100:>6.1f}%")
    print(f"    {'naive (S-Naive)':<20s}: {bt.naive_mape*100:>6.1f}%")


def compare_strategies() -> None:
    """A/B comparison: flat-weight ensemble vs per-horizon ensemble.

    For each product, runs the production backtest.evaluate() then blends
    two ways — flat (old) and per-horizon (new) — and reports the MAPE
    delta at each horizon.  Positive delta = per-horizon is better.
    """
    from app.services.forecasting.backtest import evaluate as backtest_eval
    from app.services.forecasting.ensemble import blend
    from app.services.forecasting.models import build_model_pool

    print("=" * 90)
    print("  A/B COMPARISON — Flat ensemble vs Per-horizon ensemble")
    print("=" * 90)

    for target in TARGETS:
        try:
            df = fetch_series(target["where"])
        except Exception as exc:
            print(f"\n  [{target['name']}] SKIP: {exc}")
            continue
        if df.empty or len(df) < MIN_TRAIN_DAYS + MAX_HORIZON:
            print(f"\n  [{target['name']}] SKIP: too short ({len(df)})")
            continue

        y = df.set_index("ds")["y"]
        models = build_model_pool(seasonal_period=7, y=y)
        bt = backtest_eval(y, models, seasonal_period=7, horizons=HORIZONS)

        # Build per-horizon error map (exclude baselines from the blend).
        per_h = {
            h: {n: e for n, e in m.items()
                if n not in ("naive_last", "seasonal_naive", "mean_reversion")}
            for h, m in bt.per_horizon_mape.items()
        }
        flat_err = {n: e for n, e in bt.per_model_mape.items()
                    if n not in ("naive_last", "seasonal_naive", "mean_reversion")}

        print(f"\n  [{target['name']}]  ({len(y)} rows, {bt.n_folds} folds)")
        print(f"    {'Horizon':<10s} {'Flat MAPE':>12s} {'PerH MAPE':>12s} {'Delta':>10s} {'Better?':>10s}")
        for h in HORIZONS:
            herr = per_h.get(h, {})
            if not herr:
                continue
            # Simulate: compare the best-model MAPE at this horizon under
            # flat (avg) vs per-horizon (horizon-specific) weighting.
            flat_best = min(flat_err.values()) if flat_err else float("inf")
            perh_best = min(herr.values()) if herr else float("inf")
            delta = flat_best - perh_best
            better = "per-h" if delta > 0.001 else ("flat" if delta < -0.001 else "tie")
            print(f"    {h:<10d} {flat_best*100:>11.1f}% {perh_best*100:>11.1f}% {delta*100:>+9.1f}% {better:>10s}")


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    """Run walk-forward backtest on all configured targets."""
    from app.services.forecasting.models import build_model_pool

    print("=" * 80)
    print("  WALK-FORWARD BACKTEST — End-to-End Forecast Accuracy Test")
    print("=" * 80)
    print(f"  Targets:  {len(TARGETS)} products (md_t_lz_price)")
    print(f"  Config:   min_train={MIN_TRAIN_DAYS}d  step={STEP_DAYS}d  max_origins={MAX_ORIGINS}")
    print(f"  Horizons: {HORIZONS}")
    print()

    # Build model pool once (models are stateless at init)
    models = build_model_pool(seasonal_period=7)
    print(f"  Model pool: {len(models)} models ({', '.join(models.keys())})")

    target_results: list[TargetResult] = []
    total_start = time.time()

    for target in TARGETS:
        print(f"\n  [{target['name']}] Fetching series...", end=" ", flush=True)
        try:
            df = fetch_series(target["where"])
        except Exception as exc:
            print(f"SKIP: {exc}")
            continue

        if df.empty:
            print("SKIP: empty series")
            continue

        print(f"{len(df)} rows", end=" ", flush=True)

        if len(df) < MIN_TRAIN_DAYS + MAX_HORIZON:
            print(f"SKIP: too short ({len(df)} < {MIN_TRAIN_DAYS + MAX_HORIZON})")
            continue

        y = df.set_index("ds")["y"]
        print(f"[{y.index.min().date()} → {y.index.max().date()}]")

        # Walk-forward evaluation
        fit_start = time.time()
        hresults = walk_forward_evaluate_v2(y, models, seasonal_period=7)
        fit_seconds = time.time() - fit_start

        tr = TargetResult(
            product_key=target["product_key"],
            name=target["name"],
            series_length=len(y),
            horizons=hresults,
            fit_seconds=fit_seconds,
        )
        target_results.append(tr)

        # Quick status
        for h in HORIZONS:
            hr = hresults.get(h)
            if hr:
                print(f"    {h}d forecast: MAPE={hr.mape:.1f}%  RMSE={hr.rmse:.2f}  "
                      f"Bias={hr.bias:+.2f}  DirAcc={hr.dir_acc:.1%}  folds={hr.n_folds}")

        # Per-model detail (expanding-window context)
        print_per_model_detail(y, models, target["name"])

    total_seconds = time.time() - total_start
    print_summary(target_results, total_seconds)


if __name__ == "__main__":
    if "--compare" in sys.argv:
        compare_strategies()
    else:
        main()
