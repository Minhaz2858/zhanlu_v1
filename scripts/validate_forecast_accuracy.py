#!/usr/bin/env python3
"""One-shot forecast accuracy validation script.

Runs evaluation_job.run_evaluation() to score pending ForecastRun rows,
queries ForecastAccuracyLog for historical accuracy, checks thresholds,
and produces a Markdown report.

Usage:
    cd /home/ysk2025/zhanlu_7_30/backend
    venv/bin/python scripts/validate_forecast_accuracy.py          # All products
    venv/bin/python scripts/validate_forecast_accuracy.py --days 30  # 30-day window
    venv/bin/python scripts/validate_forecast_accuracy.py --product crude_oil  # Single
    venv/bin/python scripts/validate_forecast_accuracy.py --walk-forward      # + WF backtest
    venv/bin/python scripts/validate_forecast_accuracy.py --json > report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT = Path(__file__).resolve()
_PROJECT = _SCRIPT.parents[2]
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("validate_forecast")

from app.database import SessionLocal
from app.models.forecasting import ForecastTarget, ForecastRun, ForecastAccuracyLog
from app.services.forecasting.accuracy_report import AccuracyThreshold, check_thresholds

# ── Report output path ─────────────────────────────────────────────────
_REPORT_DIR = _PROJECT.parent / "docs" / "superpowers" / "plans"


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "N/A"
    return f"{x:.1f}%"


def _fmt_float(x: float | None, precision: int = 2) -> str:
    if x is None:
        return "N/A"
    return f"{x:.{precision}f}"


# ══════════════════════════════════════════════════════════════════════════
# Phase 1: Run evaluation (score pending runs)
# ══════════════════════════════════════════════════════════════════════════

def run_eval_step(db, product_key: str | None = None) -> dict:
    """Score pending ForecastRun rows against newly-arrived actuals."""
    from app.services.forecasting.ops.evaluation_job import run_evaluation

    logger.info("[1] Running evaluation job…")
    result = run_evaluation(db, product_key=product_key)
    db.commit()
    logger.info(
        "    Scored: %d  Skipped: %d  Feedback: %d",
        result.get("runs_scored", 0),
        result.get("runs_skipped", 0),
        result.get("feedback_scored", 0),
    )
    return result


# ══════════════════════════════════════════════════════════════════════════
# Phase 2: Query recent accuracy logs
# ══════════════════════════════════════════════════════════════════════════

def query_accuracy_logs(
    db,
    days: int = 30,
    product_key: str | None = None,
) -> list[dict]:
    """Fetch recent ForecastAccuracyLog rows with realized metrics."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    q = (
        db.query(
            ForecastAccuracyLog,
            ForecastTarget.product_key,
            ForecastTarget.name,
        )
        .join(ForecastTarget, ForecastTarget.id == ForecastAccuracyLog.target_id)
        .filter(ForecastAccuracyLog.evaluated_at >= cutoff)
        .filter(ForecastAccuracyLog.realized_mape.isnot(None))
        .order_by(ForecastAccuracyLog.evaluated_at.desc())
    )

    if product_key:
        q = q.filter(ForecastTarget.product_key == product_key)

    rows = []
    for log, pk, name in q.all():
        rows.append({
            "product_key": pk,
            "name": name,
            "horizon": log.horizon_days,
            "mape": log.mape,            # backtest (ensemble) MAPE
            "naive_mape": log.naive_mape,
            "realized_mape": log.realized_mape,
            "realized_error": log.realized_error,
            "mae": log.mae,
            "rmse": log.rmse,
            "evaluated_at": log.evaluated_at,
            "below_naive": log.below_naive_baseline,
        })
    return rows


# ══════════════════════════════════════════════════════════════════════════
# Phase 3: Aggregate per-product metrics
# ══════════════════════════════════════════════════════════════════════════

def aggregate_per_product(rows: list[dict]) -> dict[str, dict]:
    """Group accuracy log rows by product_key, compute summary stats."""
    by_product: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_product[r["product_key"]].append(r)

    summary: dict[str, dict] = {}
    for pk, group in by_product.items():
        name = group[0]["name"]
        realized_mapes = [r["realized_mape"] for r in group if r["realized_mape"] is not None]
        maes = [r["mae"] for r in group if r["mae"] is not None]
        rmses = [r["rmse"] for r in group if r["rmse"] is not None]
        backtest_mapes = [r["mape"] for r in group if r["mape"] is not None]

        summary[pk] = {
            "name": name,
            "n_evaluations": len(group),
            "avg_realized_mape": sum(realized_mapes) / len(realized_mapes) if realized_mapes else None,
            "avg_mae": sum(maes) / len(maes) if maes else None,
            "avg_rmse": sum(rmses) / len(rmses) if rmses else None,
            "avg_backtest_mape": sum(backtest_mapes) / len(backtest_mapes) if backtest_mapes else None,
            "latest_realized_mape": realized_mapes[0] if realized_mapes else None,
            "latest_mae": maes[0] if maes else None,
            "latest_rmse": rmses[0] if rmses else None,
            "details": group,
        }

    return summary


# ══════════════════════════════════════════════════════════════════════════
# Phase 4: (Optional) Walk-forward backtest
# ══════════════════════════════════════════════════════════════════════════

def run_walk_forward(db, targets: list) -> dict[str, dict]:
    """Run walk-forward backtest for each target using its datasource config."""
    from app.services.forecasting.edia_source import EdiaMysqlDataSource
    from app.services.forecasting.models import build_model_pool
    from tests.walk_forward_backtest import walk_forward_evaluate_v2

    edia = EdiaMysqlDataSource()
    models = build_model_pool(seasonal_period=7)
    horizons = [7, 14, 30]

    results: dict[str, dict] = {}
    for t in targets:
        pk = t.product_key
        name = t.name or pk
        logger.info("[WF] Fetching data for %s (%s)…", pk, name)
        try:
            df = edia.read_history(t.datasource)
        except Exception as e:
            logger.warning("[WF] Skipping %s — data fetch failed: %s", pk, e)
            results[pk] = {"error": str(e)}
            continue

        if df is None or len(df) < 60:
            n = len(df) if df is not None else 0
            logger.warning("[WF] Skipping %s — insufficient data (%d rows)", pk, n)
            results[pk] = {"error": "insufficient_data", "n_rows": n}
            continue

        # Convert to Series for walk_forward_evaluate_v2
        if isinstance(df, pd.Series):
            y = df
        else:
            import pandas as pd
            time_col = "FDATE" if "FDATE" in df.columns else df.columns[0]
            measure = "FTAXPRICE" if "FTAXPRICE" in df.columns else df.columns[-1]
            y = pd.Series(df[measure].astype(float).values,
                          index=pd.to_datetime(df[time_col]))

        y = y[~y.index.duplicated(keep="last")].sort_index().dropna()
        n_rows = len(y)
        if n_rows < 90:  # MIN_TRAIN_DAYS(30) + MAX_HORIZON(30) min, use generous margin
            logger.warning("[WF] Skipping %s — %d rows < 90", pk, n_rows)
            results[pk] = {"error": "insufficient_data", "n_rows": n_rows}
            continue

        logger.info("[WF] Running walk-forward for %s (%d rows)…", pk, n_rows)
        try:
            hr = walk_forward_evaluate_v2(y, models, seasonal_period=7, horizons=horizons)
        except Exception as e:
            logger.exception("[WF] Walk-forward failed for %s: %s", pk, e)
            results[pk] = {"error": str(e)}
            continue

        results[pk] = {
            "name": name,
            "n_rows": n_rows,
            "horizons": {},
        }
        for h, r in hr.items():
            results[pk]["horizons"][h] = {
                "mape": r.mape,
                "rmse": r.rmse,
                "bias": r.bias,
                "dir_acc": r.dir_acc,
            }

    return results


# ══════════════════════════════════════════════════════════════════════════
# Phase 5: Format report
# ══════════════════════════════════════════════════════════════════════════

def format_report(
    summary: dict[str, dict],
    log_rows: list[dict],
    thresholds: AccuracyThreshold,
    days: int,
    wf_results: dict | None = None,
) -> str:
    """Produce a Markdown accuracy validation report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    lines.append(f"# Forecast Accuracy Validation Report")
    lines.append(f"**Generated**: {now}")
    lines.append(f"**Lookback window**: {days} days")
    lines.append(f"**Products evaluated**: {len(summary)}")
    lines.append(f"**Thresholds**: excellent <{thresholds.excellent}% | "
                 f"acceptable <{thresholds.acceptable}% | "
                 f"critical <{thresholds.critical}% MAPE")
    lines.append("")

    # ── Per-product table ──
    lines.append("## 1. Per-Product Accuracy Summary")
    lines.append("")
    lines.append(
        "| Product | Evals | Latest MAPE | Avg MAPE | Latest MAE | Latest RMSE | "
        "Backtest MAPE | Status |"
    )
    lines.append(
        "|---------|-------|------------|----------|------------|------------|"
        "--------------|--------|"
    )

    status_counts: dict[str, int] = defaultdict(int)
    aggregated_mapes: dict[str, float] = {}

    for pk in sorted(summary.keys()):
        s = summary[pk]
        latest_mape = s["latest_realized_mape"]
        status = thresholds.check(latest_mape)
        status_counts[status] += 1
        if latest_mape is not None:
            aggregated_mapes[pk] = latest_mape

        lines.append(
            f"| {s['name'] or pk} | {s['n_evaluations']} | "
            f"{_fmt_pct(latest_mape)} | {_fmt_pct(s['avg_realized_mape'])} | "
            f"{_fmt_float(s['latest_mae'])} | {_fmt_float(s['latest_rmse'])} | "
            f"{_fmt_pct(s['avg_backtest_mape'])} | **`{status}`** |"
        )

    lines.append("")

    # ── Threshold distribution ──
    lines.append("## 2. Threshold Distribution")
    lines.append("")
    for status in ["excellent", "acceptable", "critical", "blocked", "unknown"]:
        count = status_counts.get(status, 0)
        if count > 0:
            lines.append(f"- **{status}**: {count} product(s)")
    lines.append("")

    # ── Aggregate summary ──
    lines.append("## 3. Aggregate Metrics")
    lines.append("")

    all_mapes = [s["avg_realized_mape"] for s in summary.values()
                 if s["avg_realized_mape"] is not None]
    all_maes = [s["avg_mae"] for s in summary.values()
                if s["avg_mae"] is not None]
    all_rmses = [s["avg_rmse"] for s in summary.values()
                 if s["avg_rmse"] is not None]

    lines.append(f"- **Overall avg realized MAPE**: {_fmt_pct(sum(all_mapes)/len(all_mapes)) if all_mapes else 'N/A'}")
    lines.append(f"- **Overall avg MAE**: {_fmt_float(sum(all_maes)/len(all_maes)) if all_maes else 'N/A'}")
    lines.append(f"- **Overall avg RMSE**: {_fmt_float(sum(all_rmses)/len(all_rmses)) if all_rmses else 'N/A'}")
    lines.append(f"- **Total evaluation rows**: {len(log_rows)}")
    lines.append("")

    # ── Threshold-based aggregation ──
    threshold_result = check_thresholds(aggregated_mapes, thresholds)
    lines.append("## 4. Threshold Check Summary")
    lines.append("")
    for pk, info in sorted(threshold_result.items()):
        name = summary.get(pk, {}).get("name", pk)
        lines.append(f"- **{name}** (`{pk}`): MAPE={_fmt_pct(info['mape'])} → `{info['status']}`")
    lines.append("")

    # ── Walk-forward (optional) ──
    if wf_results:
        lines.append("## 5. Walk-Forward Backtest (Forward-Looking)")
        lines.append("")
        for pk, wf in sorted(wf_results.items()):
            if "error" in wf:
                lines.append(f"- **{pk}**: SKIPPED ({wf['error']})")
                continue
            lines.append(f"### {wf.get('name', pk)} (n={wf.get('n_rows', '?')})`")
            lines.append("")
            lines.append("| Horizon | MAPE | RMSE | Bias | Dir. Accuracy |")
            lines.append("|---------|------|------|------|---------------|")
            for h in sorted(wf.get("horizons", {}).keys()):
                r = wf["horizons"][h]
                lines.append(
                    f"| {h}d | {_fmt_pct(r['mape'])} | {_fmt_float(r['rmse'])} | "
                    f"{_fmt_float(r['bias'], 4)} | {_fmt_pct(r['dir_acc'])} |"
                )
            lines.append("")

    # ── Recommendations ──
    blocked_or_critical = [
        pk for pk, info in threshold_result.items()
        if info["status"] in ("critical", "blocked")
    ]
    if blocked_or_critical:
        lines.append("## 6. Action Items (Products Below Threshold)")
        lines.append("")
        for pk in blocked_or_critical:
            status = threshold_result[pk]["status"]
            lines.append(f"### {summary.get(pk, {}).get('name', pk)} (`{status}`)")
            lines.append("")
            lines.append("- [ ] Review discrepancy report for this product")
            lines.append("- [ ] Check XGBoost tuning — consider re-tuning via `tune_xgboost_params()`")
            lines.append("- [ ] Verify ensemble weights — check if regime-aware pool is appropriate")
            lines.append("- [ ] Review feature freshness — re-run technical indicators + Fourier features")
            lines.append("- [ ] Consider enabling/disabling Wave 6 features (stacking, VAR)")
            lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate forecast accuracy and generate report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  venv/bin/python scripts/validate_forecast_accuracy.py\n"
               "  venv/bin/python scripts/validate_forecast_accuracy.py --days 14\n"
               "  venv/bin/python scripts/validate_forecast_accuracy.py --product crude_oil --walk-forward",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Lookback window in days for accuracy log queries (default: 30).",
    )
    parser.add_argument(
        "--product",
        help="Only validate a single product_key.",
    )
    parser.add_argument(
        "--walk-forward", action="store_true",
        help="Also run walk-forward backtest (slow! adds ~5-10 min).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of Markdown report.",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="Do not save report file.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # Step 1: Run evaluation to score pending runs
        eval_result = run_eval_step(db, product_key=args.product)

        # Step 2: Query accuracy logs
        log_rows = query_accuracy_logs(db, days=args.days, product_key=args.product)
        logger.info("[2] Fetched %d accuracy log rows.", len(log_rows))

        if not log_rows:
            print("# No accuracy logs found in the lookback window.")
            print("# Ensure forecast runs exist and actual data has arrived for the horizon.")
            return 0

        # Step 3: Aggregate
        summary = aggregate_per_product(log_rows)
        thresholds = AccuracyThreshold()
        logger.info("[3] Aggregated %d product(s).", len(summary))

        # Step 4: Optional walk-forward
        wf_results = None
        if args.walk_forward:
            logger.warning("[4] Walk-forward backtest requested — this may take several minutes.")
            targets = (
                db.query(ForecastTarget)
                .filter(ForecastTarget.is_deleted == False)
                .all()
            )
            if args.product:
                targets = [t for t in targets if t.product_key == args.product]
            wf_results = run_walk_forward(db, targets)

        # Step 5: JSON or Markdown output
        if args.json:
            report = {
                "eval": eval_result,
                "n_log_rows": len(log_rows),
                "summary": summary,
                "thresholds": {
                    "excellent": thresholds.excellent,
                    "acceptable": thresholds.acceptable,
                    "critical": thresholds.critical,
                },
                "threshold_check": check_thresholds(
                    {pk: s["latest_realized_mape"] for pk, s in summary.items()
                     if s["latest_realized_mape"] is not None},
                    thresholds,
                ),
                "walk_forward": wf_results,
            }
            print(json.dumps(report, indent=2, default=str))
        else:
            report_md = format_report(summary, log_rows, thresholds, args.days, wf_results)
            print(report_md)
            if not args.no_save:
                date_str = datetime.now().strftime("%Y-%m-%d")
                out_dir = _REPORT_DIR
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{date_str}-accuracy-report.md"
                out_path.write_text(report_md, encoding="utf-8")
                logger.info("Report saved to %s", out_path)

        return 0

    except Exception:
        logger.exception("Validation failed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
