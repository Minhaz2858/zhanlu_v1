#!/usr/bin/env python3
"""Test-run the forecast engine with warehouse Tier 3 loaders active.

Runs compute_target_anchored for each product, captures timing, metrics,
and whether warehouse data was loaded. Compares with baseline (7d MAPE=11.4%).
"""
import logging
import sys
import time
import json

# Enable INFO logging to see warehouse fallback messages
logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
# Suppress noisy loggers
for name in ["sqlalchemy", "urllib3", "chromadb", "sentence_transformers", "httpx", "httpcore"]:
    logging.getLogger(name).setLevel(logging.WARNING)

sys.path.insert(0, "/app")

from app.db.base import SessionLocal
from app.models.forecast import ForecastTarget, ForecastRun
from app.services.forecasting.engine import ForecastEngine
from app.services.forecasting.seed_targets import seed_forecast_targets

PRODUCTS = ["isoprene", "piperylene", "dcpd", "cracked_c5", "cracked_c9", "styrene", "sis", "blowing_agent"]
HORIZONS = [7, 14, 30]


def main():
    db = SessionLocal()
    print("=" * 100)
    print("FORECAST TEST RUN — Warehouse Tier 3 Loaders Active")
    print("=" * 100)

    # Step 1: Seed targets
    print("\n[1] Seeding forecast targets...")
    seed_forecast_targets(db)
    db.commit()

    targets = db.query(ForecastTarget).filter(ForecastTarget.is_deleted == False).all()
    print(f"    Found {len(targets)} targets")

    results = []

    for target in targets:
        pid = target.product_id
        print(f"\n{'─'*100}")
        print(f"[2] Forecasting: {pid} (target_id={target.id})")
        print(f"{'─'*100}")

        engine = ForecastEngine(db_session=db)
        t0 = time.time()
        try:
            result = engine.compute_target_anchored(
                target.id,
                horizons=HORIZONS,
            )
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            results.append({"product": pid, "error": str(exc)[:200]})
            continue
        elapsed = time.time() - t0

        if result is None:
            print(f"  Target not found, skipping")
            continue

        run = result.get("run")
        sample_size = result.get("sample_size", 0)
        source_table = result.get("source_table", "?")

        print(f"  Source: {source_table}  Sample size: {sample_size}  Time: {elapsed:.1f}s")

        if run is None:
            print(f"  Run: None (compute failed or insufficient data)")
            results.append({"product": pid, "sample_size": sample_size, "run": None, "time": elapsed})
            continue

        # Print forecast values
        fc = run.forecast_values or {}
        print(f"\n  Forecast values (horizon → price):")
        for h in HORIZONS:
            vals = fc.get(str(h), fc.get(h, []))
            if isinstance(vals, list) and vals:
                first = vals[0] if vals else None
                last = vals[-1] if vals else None
                print(f"    h={h:>2}d: {len(vals)} points  first={first}  last={last}")
            elif isinstance(vals, (int, float)):
                print(f"    h={h:>2}d: {vals}")
            else:
                print(f"    h={h:>2}d: {type(vals).__name__} = {str(vals)[:80]}")

        # Backtest metrics
        bt = run.backtest_metrics or {}
        mape = bt.get("mape", bt.get("aggregate_mape", {}))
        dir_acc = bt.get("directional_accuracy", bt.get("dir_acc", {}))
        below_naive = run.below_naive_baseline
        confidence = run.confidence

        print(f"\n  Backtest metrics:")
        print(f"    MAPE: {mape}")
        print(f"    DirAcc: {dir_acc}")
        print(f"    Below naive: {below_naive}")
        print(f"    Confidence: {confidence}")

        # Explanation (includes exogenous features, signals, analyst brief)
        expl = run.explanation or {}
        if expl:
            print(f"\n  Explanation keys: {list(expl.keys())}")

            # Check for exogenous features (warehouse data)
            exog = expl.get("exogenous_features", expl.get("exog", {}))
            if exog:
                print(f"\n  Exogenous features:")
                for k, v in exog.items():
                    if isinstance(v, dict):
                        print(f"    {k}: {list(v.keys())[:5]}...")
                    else:
                        print(f"    {k}: {str(v)[:80]}")

            # Check for demand signal / supply-demand signals
            signals = expl.get("signals", expl.get("demand_signal", {}))
            if signals:
                print(f"\n  Signals:")
                print(f"    {json.dumps(signals, ensure_ascii=False, default=str)[:300]}")

            # Analyst brief
            brief = expl.get("analyst_brief", {})
            if brief:
                print(f"\n  Analyst brief (first day):")
                first_brief = brief.get("7", brief.get(7, brief.get("1", {})))
                if isinstance(first_brief, dict):
                    for k, v in first_brief.items():
                        print(f"    {k}: {str(v)[:120]}")

        results.append({
            "product": pid,
            "sample_size": sample_size,
            "time": elapsed,
            "mape": mape,
            "dir_acc": dir_acc,
            "confidence": confidence,
            "below_naive": below_naive,
            "run_id": run.id,
        })

        # Commit after each product to persist results
        db.commit()

    # Summary
    print(f"\n\n{'='*100}")
    print("SUMMARY")
    print(f"{'='*100}")
    print(f"\n{'Product':<20} {'Time':>6}  {'Sample':>6}  {'MAPE':>20}  {'DirAcc':>15}  {'Conf':>8}")
    print("-" * 85)
    for r in results:
        pid = r.get("product", "?")
        t = f"{r.get('time', 0):.1f}s"
        ss = str(r.get("sample_size", 0))
        mape = str(r.get("mape", "N/A"))[:20]
        da = str(r.get("dir_acc", "N/A"))[:15]
        conf = str(r.get("confidence", "?"))[:8]
        print(f"{pid:<20} {t:>6}  {ss:>6}  {mape:>20}  {da:>15}  {conf:>8}")

    # Baseline comparison
    print(f"\nBaseline (2026-08-06, no warehouse data):")
    print(f"  7d MAPE=11.4%  14d MAPE=17.3%  30d MAPE=23.5%")
    print(f"  DirAcc=24-38% (below 50% random)")

    db.close()
    print("\n# Done.")


if __name__ == "__main__":
    main()
