"""Forecast accuracy reporting: threshold checks and discrepancy reports."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

# ── Settings-backed defaults ──────────────────────────────────────


@dataclass
class AccuracyThreshold:
    """Tiered accuracy thresholds configurable via settings flags.

    Defaults:
        excellent  = 8.0% MAPE  (FORECAST_ACCURACY_THRESHOLD_EXCELLENT)
        acceptable = 15.0% MAPE  (FORECAST_ACCURACY_THRESHOLD_ACCEPTABLE)
        critical   = 25.0% MAPE  (FORECAST_ACCURACY_THRESHOLD_CRITICAL)
    """

    excellent: float = field(
        default_factory=lambda: float(getattr(settings, "FORECAST_ACCURACY_THRESHOLD_EXCELLENT", 8.0))
    )
    acceptable: float = field(
        default_factory=lambda: float(getattr(settings, "FORECAST_ACCURACY_THRESHOLD_ACCEPTABLE", 15.0))
    )
    critical: float = field(
        default_factory=lambda: float(getattr(settings, "FORECAST_ACCURACY_THRESHOLD_CRITICAL", 25.0))
    )

    def check(self, mape: float | None) -> str:
        """Classify a MAPE value into one of:
        'unknown', 'excellent', 'acceptable', 'critical', 'blocked'.
        """
        if mape is None:
            return "unknown"
        if mape < self.excellent:
            return "excellent"
        if mape < self.acceptable:
            return "acceptable"
        if mape < self.critical:
            return "critical"
        return "blocked"


def check_thresholds(
    product_mapes: dict[str, float],
    threshold: AccuracyThreshold | None = None,
) -> dict[str, dict]:
    """Check thresholds for multiple products.

    Args:
        product_mapes: {product_key: mape_value}
        threshold: optional custom threshold; uses env defaults if None.

    Returns:
        {product_key: {"mape": float, "status": str}}
    """
    if threshold is None:
        threshold = AccuracyThreshold()

    results: dict[str, dict] = {}
    for product_key, mape in product_mapes.items():
        status = threshold.check(mape)
        results[product_key] = {"mape": mape, "status": status}

    return results


# ── Discrepancy report ────────────────────────────────────────────

import pandas as pd
import numpy as np


def _build_day_level_comparison(
    forecast_values: list[float],
    forecast_dates: list[pd.Timestamp],
    actual_values: pd.Series | None,
) -> pd.DataFrame:
    """Build per-day comparison DataFrame.

    Returns DataFrame with columns: date, predicted, actual, residual, pct_error.
    Empty DataFrame if no actuals match.
    """
    if actual_values is None or len(actual_values) == 0:
        return pd.DataFrame(columns=["date", "predicted", "actual", "residual", "pct_error"])

    rows = []
    for i, fdate in enumerate(forecast_dates):
        actual_val = actual_values.get(fdate) if fdate in actual_values.index else None
        predicted = forecast_values[i]
        if actual_val is not None:
            residual = predicted - actual_val
            pct_error = (abs(residual) / actual_val * 100) if actual_val > 0 else None
        else:
            residual = None
            pct_error = None
        rows.append({
            "date": fdate,
            "predicted": predicted,
            "actual": actual_val,
            "residual": residual,
            "pct_error": pct_error,
        })

    df = pd.DataFrame(rows)
    # Keep only rows that have actual values for meaningful comparison
    if "actual" in df.columns:
        df = df[df["actual"].notna()].copy()
    return df


def _generate_root_cause_hypotheses(
    df: pd.DataFrame,
    horizon_days: int = 7,
) -> list[str]:
    """Analyze discrepancy patterns and generate root-cause hypotheses.

    Returns a list of human-readable hypothesis strings, or empty list
    if no clear pattern is detected.
    """
    hypotheses: list[str] = []

    if df.empty or "pct_error" not in df.columns:
        return ["No actual data available for comparison — unable to generate hypotheses."]

    pct_errors = df["pct_error"].dropna()
    residuals = df["residual"].dropna()

    if len(pct_errors) == 0:
        return ["All forecast dates matched but no valid actual values found."]

    mean_pct = float(pct_errors.mean())
    max_pct = float(pct_errors.max())
    mean_residual = float(residuals.mean())

    # 1. Overall accuracy assessment
    if mean_pct < 8.0:
        hypotheses.append(
            f"Forecast accuracy is good (avg {mean_pct:.1f}% MAPE) — no major discrepancies detected."
        )
    else:
        hypotheses.append(
            f"Overall MAPE is {mean_pct:.1f}%, indicating meaningful forecast error across "
            f"the {horizon_days}-day horizon."
        )

    # 2. Bias direction
    if abs(mean_residual) < 1e-6:
        hypotheses.append("Model is nearly unbiased — errors are balanced around zero.")
    elif mean_residual > 0:
        hypotheses.append(
            f"Model shows positive bias (avg residual +{mean_residual:.2f}) — "
            f"tends to **overpredict** prices. Consider bias-correction or reviewing "
            f"ensemble weight on momentum-heavy models (xgboost_reg)."
        )
    else:
        hypotheses.append(
            f"Model shows negative bias (avg residual {mean_residual:.2f}) — "
            f"tends to **underpredict** prices. Consider reducing conservative baselines "
            f"(naive, seasonal_naive) weight in the ensemble."
        )

    # 3. Pattern detection: spike / large single-day error
    if max_pct > 20.0 and len(pct_errors) >= 3:
        # Find days with large errors
        large_error_days = [
            str(row["date"].date()) for _, row in df.iterrows()
            if row.get("pct_error") is not None and row["pct_error"] > 15.0
        ]
        if large_error_days:
            hypotheses.append(
                f"**Large error spike** detected on: {', '.join(large_error_days[:5])}. "
                f"These may coincide with exogenous events (feedstock price shock, supply disruption, "
                f"policy announcement). Cross-reference with upstream market data and intelligence feed."
            )

    # 4. Trend in errors: worsening over horizon
    if len(pct_errors) >= 4:
        first_half = pct_errors.iloc[: len(pct_errors) // 2].mean()
        second_half = pct_errors.iloc[len(pct_errors) // 2:].mean()
        if second_half > first_half * 1.5:
            hypotheses.append(
                f"Error magnitude increases over the horizon "
                f"(early {first_half:.1f}% → late {second_half:.1f}% MAPE). "
                f"Typical of mean-reversion or naive baselines dominating longer-horizon forecasts. "
                f"Consider XGBoost Direct multi-step model for better long-range accuracy."
            )

    # 5. Directional accuracy
    if len(df) >= 3:
        directions = []
        for i in range(1, len(df)):
            pred_dir = df.iloc[i]["predicted"] - df.iloc[i - 1]["actual"]
            actual_dir = df.iloc[i]["actual"] - df.iloc[i - 1]["actual"]
            if pred_dir is not None and actual_dir is not None:
                directions.append((pred_dir > 0) == (actual_dir > 0))
        if directions:
            dir_acc = sum(directions) / len(directions)
            if dir_acc < 0.5:
                hypotheses.append(
                    f"Directional accuracy is low ({dir_acc:.0%}) — model is essentially "
                    f"guessing direction. May need regime-aware ensemble or macro-override active."
                )

    # 6. Comparison to naive baseline
    naive_errors = []
    for i in range(1, len(df)):
        naive_pred = df.iloc[i - 1]["actual"]  # naive = last actual
        actual = df.iloc[i]["actual"]
        if naive_pred is not None and actual is not None:
            naive_errors.append(abs(naive_pred - actual) / actual * 100)
    if naive_errors:
        naive_mape = float(np.mean(naive_errors))
        if mean_pct > naive_mape:
            hypotheses.append(
                f"Forecast accuracy ({mean_pct:.1f}%) is **worse than naive baseline** "
                f"({naive_mape:.1f}%). This is a red flag — check if data pipeline is "
                f"feeding stale or incorrect data into the model."
            )

    return hypotheses


def generate_discrepancy_report(
    db,
    product_key: str,
    horizon_days: int = 7,
) -> dict | None:
    """Generate a full discrepancy report for a product.

    Fetches the most recent ForecastRun for the product, extracts forecast
    values, loads actuals from the datasource, and builds a per-day comparison
    with root-cause hypotheses.

    Returns:
        dict with keys: product_key, run_id, as_of_date, horizon_days,
        summary (mape, mae, rmse, bias, dir_acc), day_detail (DataFrame dict),
        hypotheses (list[str]), recommendations (list[str])
        Returns None if no suitable ForecastRun is found.
    """
    from app.models.forecasting import ForecastTarget, ForecastRun

    target = (
        db.query(ForecastTarget)
        .filter(
            ForecastTarget.product_key == product_key,
            ForecastTarget.is_deleted == False,
        )
        .first()
    )
    if target is None:
        logger.warning("No ForecastTarget found for product_key=%s", product_key)
        return None

    run = (
        db.query(ForecastRun)
        .filter(ForecastRun.target_id == target.id)
        .order_by(ForecastRun.as_of_date.desc())
        .first()
    )
    if run is None:
        logger.warning("No ForecastRun found for target_id=%s", target.id)
        return None

    results = run.results or {}
    key = str(horizon_days)
    if key not in results:
        logger.warning("No %d-day results in ForecastRun %s", horizon_days, run.id)
        return None

    base_vals = results[key].get("base") if isinstance(results[key], dict) else None
    if not base_vals:
        logger.warning("No base forecast values for horizon %d", horizon_days)
        return None

    # Reconstruct forecast dates (daily cadence)
    as_of = run.as_of_date or run.created_date
    if as_of is None:
        logger.warning("ForecastRun has no as_of_date")
        return None

    if hasattr(as_of, "tzinfo") and as_of.tzinfo is not None:
        from datetime import timezone as _tz
        as_of = as_of.astimezone(_tz.utc).replace(tzinfo=None)

    from datetime import timedelta
    forecast_dates = [
        pd.Timestamp(as_of + timedelta(days=i))
        for i in range(1, len(base_vals) + 1)
    ][:horizon_days]

    # Fetch actuals
    from app.services.forecasting.mysql_data_source import MysqlDataSource
    try:
        edia = MysqlDataSource()
        df = edia.read_history(target.datasource)
    except Exception as e:
        logger.exception("Failed to load actuals for %s: %s", product_key, e)
        return {
            "product_key": product_key,
            "run_id": run.id,
            "as_of_date": str(as_of),
            "horizon_days": horizon_days,
            "error": f"Actuals load failed: {e}",
        }

    if df is None or len(df) == 0:
        return {
            "product_key": product_key,
            "run_id": run.id,
            "as_of_date": str(as_of),
            "horizon_days": horizon_days,
            "error": "No actual data available.",
        }

    # Build actuals Series
    if isinstance(df, pd.Series):
        actual = df
    else:
        time_col = "FDATE" if "FDATE" in df.columns else df.columns[0]
        measure = "FTAXPRICE" if "FTAXPRICE" in df.columns else df.columns[-1]
        actual = pd.Series(
            df[measure].astype(float).values,
            index=pd.to_datetime(df[time_col]),
        )
    actual = actual[~actual.index.duplicated(keep="last")].sort_index()
    if getattr(actual.index, "tz", None) is not None:
        actual.index = actual.index.tz_convert(None)

    # Build comparison
    day_df = _build_day_level_comparison(
        base_vals[:horizon_days], forecast_dates, actual
    )

    # Compute summary metrics
    from app.services.forecasting.accuracy_tracker import compute_realized_error
    metrics = compute_realized_error(base_vals[:horizon_days], forecast_dates, actual)

    # Generate hypotheses
    hypotheses = _generate_root_cause_hypotheses(day_df, horizon_days)

    # Generate recommendations based on status
    threshold = AccuracyThreshold()
    status = threshold.check(metrics.get("mape"))
    recommendations = _generate_recommendations(status, metrics, hypotheses)

    return {
        "product_key": product_key,
        "target_name": target.name or product_key,
        "run_id": run.id,
        "as_of_date": str(as_of),
        "horizon_days": horizon_days,
        "summary": {
            "mape": metrics.get("mape"),
            "mae": metrics.get("mae"),
            "rmse": metrics.get("rmse"),
            "signed_error": metrics.get("signed_error"),
            "n_matched": metrics.get("n_matched", 0),
        },
        "status": status,
        "day_detail": day_df.to_dict(orient="records") if not day_df.empty else [],
        "hypotheses": hypotheses,
        "recommendations": recommendations,
    }


def _generate_recommendations(
    status: str,
    metrics: dict,
    hypotheses: list[str],
) -> list[str]:
    """Generate actionable recommendations based on threshold status and metrics."""
    recs: list[str] = []

    if status == "excellent":
        recs.append("Accuracy is within excellent range. Continue monitoring.")
        return recs

    if status == "acceptable":
        recs.append("Accuracy is acceptable. Monitor for degradation trends.")
        # Check if near boundary
        mape = metrics.get("mape")
        if mape is not None and mape > 12.0:
            recs.append("MAPE trending toward critical — consider preemptive XGBoost re-tuning.")
        return recs

    # critical or blocked
    recs.append("**MAPE exceeds acceptable threshold** — immediate action recommended.")
    recs.append("1. Run XGBoost hyperparameter re-tuning: `tune_xgboost_params(product_key)`")
    recs.append("2. Re-evaluate ensemble weights — check if regime-aware pool is selecting appropriate models.")

    mae = metrics.get("mae")
    rmse = metrics.get("rmse")
    if mae is not None and rmse is not None and mae > 0:
        cv = rmse / mae
        if cv > 3.0:
            recs.append(
                f"High error variability (CV={cv:.1f}) — large outliers present. "
                f"Check for data quality issues or single-day shocks in the forecast window."
            )

    bias = metrics.get("signed_error")
    if bias is not None and abs(bias) > 0.05:
        direction = "overpredicting" if bias > 0 else "underpredicting"
        recs.append(
            f"Significant bias detected ({bias:.3f}, {direction}). "
            f"Enable bias correction or adjust trend-sensitive model weights."
        )

    n = metrics.get("n_matched", 0)
    if n < 3:
        recs.append(
            f"Only {n} actual data points matched — wait for more actuals to arrive "
            f"before drawing conclusions."
        )

    recs.append("4. Review forecast feature freshness — stale technical indicators or missing upstream market data.")
    recs.append("5. Consider enabling/disabling Wave 6 features (FORECAST_STACKING, FORECAST_REGIME_AWARE_POOL, FORECAST_VAR).")

    return recs
