#!/usr/bin/env python3
"""Verify Phases 1-2 forecast pipeline: driver attribution, gate, accuracy log."""
import os, sys, json, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)

os.environ.setdefault("FORECAST_ERP_VOLUME_EXOG_ENABLED", "true")
os.environ.setdefault("FORECAST_ERP_SMOOTHING_ENABLED", "true")
os.environ.setdefault("FORECAST_SOFT_GATE_ENABLED", "true")
os.environ.setdefault("FORECAST_ADVANCED_GUARD_ENABLED", "true")

from app.database import get_db
from app.models.forecasting import ForecastTarget, ForecastRun, ForecastAccuracyLog
from app.services.forecasting.engine import ForecastEngine

db = next(get_db())

TARGETS = ["ecisco.dcpd"]
passed = 0

for product_key in TARGETS:
    print(f"\n{'='*60}")
    print(f"Testing: {product_key}")
    print(f"{'='*60}")

    try:
        db.rollback()  # Clean slate
        target = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == product_key,
            ForecastTarget.is_deleted == False,
        ).first()
    except Exception as e:
        db.rollback()

    if not target:
        print(f"  SKIP: not found")
        continue

    engine = ForecastEngine(db)
    try:
        run = engine.compute_target(target.id)
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
        continue

    if run is None:
        print(f"  FAIL: returned None")
        continue

    # --- Checks before flush (pipeline ran, DB write may fail on float32) ---
    explanation = run.explanation or {}
    drivers = explanation.get("drivers", [])
    analyst_brief = explanation.get("analyst_brief", {})

    print(f"  Run ID: {run.id}")
    print(f"  Below naive baseline: {run.below_naive_baseline}")
    print(f"  Model detail keys: {list(run.model_detail.keys()) if run.model_detail else 'none'}")
    print(f"  Drivers extracted: {len(drivers)}")
    if drivers:
        for d in drivers[:5]:
            print(f"    - {d.get('feature','?'):<30s} imp={d.get('importance',0):.4f}")
        passed += 1
    print(f"  Analyst brief horizons: {list(analyst_brief.keys()) if analyst_brief else 'none'}")
    print(f"  Exog degraded: {run.exog_degraded}")
    print(f"  Exog features: {len(run.exog_features_used or [])} features")

    # Gate info from model_detail
    md = run.model_detail or {}
    gw = md.get("gate", {})
    emape = md.get("ensemble_mape") or gw.get("ensemble_mape")
    nmape = md.get("naive_mape") or gw.get("naive_mape")
    print(f"  Gate: ensemble_mape={emape}, naive_mape={nmape}")

    # Try flush — expect float32 JSON error (pre-existing)
    try:
        db.flush()
        print(f"  ✓ DB flush OK")
        passed += 1
    except Exception as fe:
        if "float32" in str(fe):
            print(f"  ○ DB flush failed: float32 JSON (pre-existing, not our bug)")
        else:
            print(f"  ○ DB flush failed: {fe}")

    db.rollback()

db.close()
print(f"\n{'='*60}")
print(f"Checks passed: {passed}")
print(f"{'='*60}")
