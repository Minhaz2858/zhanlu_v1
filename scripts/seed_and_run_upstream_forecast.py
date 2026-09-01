"""One-shot script: seed Ecisco forecast targets + run full pipeline per target.

Used to populate ForecastRun rows when the nightly cron hasn't run yet, so that
the dashboard chart (/forecast-chart/{product_id}) can show forecast data.

Default: upstream products only. Pass --keys to choose specific targets
(comma-separated product keys) or --all for every seeded target.
"""
import argparse
import sys
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("seed_run_forecast")

from app.database import SessionLocal
from app.models.forecasting import ForecastTarget, ForecastRun
from app.services.forecasting.engine import ForecastEngine
from app.services.forecasting.seed_ecisco_targets import seed_ecisco_forecast_targets

ORG_ID = "default-org"
APP_ID = "default-app"
# Upstream-only for the dashboard chart
UPSTREAM_KEYS = {"ecisco.crude_oil", "ecisco.naphtha", "ecisco.cracked_c5"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keys",
        help="Comma-separated ForecastTarget product_keys to run (default: upstream 3).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every seeded target for the org (overrides --keys).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # 1. Seed all 12 targets (idempotent)
        logger.info("Seeding Ecisco forecast targets for org=%s…", ORG_ID)
        inserted = seed_ecisco_forecast_targets(db, org_id=ORG_ID, app_id=APP_ID)
        logger.info("Seed: %d new target(s) inserted.", inserted)

        # 2. Select targets: --all → every seeded target; --keys → explicit
        #    list; default → upstream 3 (original behaviour).
        query = db.query(ForecastTarget).filter(
            ForecastTarget.org_id == ORG_ID,
            ForecastTarget.is_deleted == False,  # noqa: E712
        )
        if args.all:
            pass
        elif args.keys:
            wanted = {k.strip() for k in args.keys.split(",") if k.strip()}
            query = query.filter(ForecastTarget.product_key.in_(wanted))
        else:
            query = query.filter(ForecastTarget.product_key.in_(UPSTREAM_KEYS))
        targets = query.all()
        if not targets:
            logger.error("No matching ForecastTarget rows found after seed.")
            return 1

        logger.info("Found %d target(s): %s",
                    len(targets), [t.product_key for t in targets])

        # 3. Run forecast for each upstream target
        engine = ForecastEngine(db)
        summary = []
        for t in targets:
            logger.info("Computing forecast for %s (target_id=%s)…", t.product_key, t.id)
            try:
                result = engine.compute_target_anchored(
                    t.id,
                    horizons=[3, 7, 30],
                    seasonal_period=7,
                )
                if result is None:
                    db.rollback()
                    logger.warning("compute_target_anchored returned None for %s", t.product_key)
                    summary.append({"target": t.product_key, "ok": False, "reason": "returned None"})
                    continue

                # Engine uses flush() only — caller must commit.
                db.commit()

                run = result.get("run")
                if run is None:
                    logger.warning("No run produced for %s", t.product_key)
                    summary.append({"target": t.product_key, "ok": False, "reason": "no run"})
                    continue

                # Inspect what got written
                horizon_30 = (run.results or {}).get("30d", {}) or {}
                summary.append({
                    "target": t.product_key,
                    "ok": True,
                    "run_id": run.id,
                    "as_of_date": run.as_of_date.isoformat() if run.as_of_date else None,
                    "confidence": run.confidence,
                    "below_naive_baseline": run.below_naive_baseline,
                    "horizon_30d_keys": list(horizon_30.keys()),
                    "horizon_30d_base_len": len(horizon_30.get("base", []) or []),
                    "horizon_30d_bull_len": len(horizon_30.get("bull", []) or []),
                    "horizon_30d_bear_len": len(horizon_30.get("bear", []) or []),
                    "horizon_30d_base_first3": (horizon_30.get("base", []) or [])[:3],
                })
            except Exception as e:
                logger.exception("Forecast failed for %s: %s", t.product_key, e)
                summary.append({"target": t.product_key, "ok": False, "error": str(e)})

        logger.info("===== SUMMARY =====")
        print(json.dumps(summary, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
