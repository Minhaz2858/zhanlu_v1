#!/usr/bin/env python3
"""
One-shot hindcast backfill: walk-forward forecast runs for the past ~30 days.

Creates ForecastRuns with the same 8-model ensemble, but trained ONLY on
historical data up to each weekly origin date (no look-ahead bias), and
with intel / domain-signals / exog overlays disabled.

Usage (dry-run):
    docker exec zhanlu-backend python scripts/backfill_forecast_runs.py --dry-run

Usage (live):
    docker exec zhanlu-backend \
        FORECAST_EXOG_ENABLED=false \
        FORECAST_INTELLIGENCE_OVERLAY_ENABLED=false \
        FORECAST_DOMAIN_SIGNALS_ENABLED=false \
        python scripts/backfill_forecast_runs.py

Usage (with custom origins):
    docker exec zhanlu-backend \
        FORECAST_EXOG_ENABLED=false \
        ... \
        python scripts/backfill_forecast_runs.py --origins 2026-07-06,2026-07-20
"""

import argparse
import json
import logging
import os
import sys
from datetime import date as dt_date, datetime, timedelta, timezone

# Ensure the backend package root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.forecasting import ForecastTarget, ForecastRun
from app.services.forecasting.engine import ForecastEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_forecast")

# ── Product-level targets (SKU sub-targets like ecisco.c5_resin.* excluded) ──
PRODUCT_TARGET_KEYS = [
    "ecisco.crude_oil",
    "ecisco.naphtha",
    "ecisco.cracked_c5",
    "ecisco.mixed_c5",
    "ecisco.cracked_c9",
    "ecisco.isoprene",
    "ecisco.piperylene",
    "ecisco.dcpd",
    "ecisco.sis",
    "ecisco.styrene",
    "ecisco.blowing_agent",
    "ecisco.c5_resin",
    "ecisco.raffinate_c5",
]

DEFAULT_ORIGINS = [
    "2026-07-06",
    "2026-07-13",
    "2026-07-20",
    "2026-07-27",
]

DEFAULT_HORIZONS = [3, 7, 15, 30]
IDEMPOTENT_WINDOW_DAYS = 1  # skip if a run exists within ±N days of origin


def parse_origins(raw: str) -> list[str]:
    """Parse comma-separated date strings."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def run_exists_for_origin(db, target_id: str, origin_date: dt_date) -> bool:
    """Check if a ForecastRun already exists within ±IDEMPOTENT_WINDOW_DAYS of origin."""
    window_start = origin_date - timedelta(days=IDEMPOTENT_WINDOW_DAYS)
    window_end = origin_date + timedelta(days=IDEMPOTENT_WINDOW_DAYS)
    existing = db.query(ForecastRun).filter(
        ForecastRun.target_id == target_id,
        ForecastRun.created_date >= window_start,
        ForecastRun.created_date <= window_end + timedelta(days=1),  # inclusive upper bound
        ForecastRun.is_deleted == False,  # noqa: E712
    ).first()
    return existing is not None


def backfill(
    origins: list[str],
    target_keys: list[str],
    horizons: list[int],
    dry_run: bool = False,
) -> dict:
    """Run the backfill. Returns a summary dict."""
    summary: dict = {
        "dry_run": dry_run,
        "origins": origins,
        "horizons": horizons,
        "results": [],
        "total_requested": len(origins) * len(target_keys),
        "total_created": 0,
        "total_skipped": 0,
        "total_errors": 0,
    }

    db = SessionLocal()
    try:
        # Resolve target keys → target rows
        targets = db.query(ForecastTarget).filter(
            ForecastTarget.product_key.in_(target_keys),
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).all()

        key_to_target_id: dict[str, str] = {
            t.product_key: t.id for t in targets
        }

        missing_keys = set(target_keys) - set(key_to_target_id.keys())
        if missing_keys:
            logger.warning(
                "Skipping %d target keys not found in DB: %s",
                len(missing_keys), missing_keys,
            )
            for mk in missing_keys:
                summary["results"].append({
                    "target_key": mk,
                    "origin": "N/A",
                    "status": "skipped",
                    "reason": "target not in DB",
                })
                summary["total_skipped"] += 1

        engine = ForecastEngine(db)
        # The engine does NOT commit — the caller (this script) commits per run
        # to keep partial progress on errors.

        for origin_str in origins:
            origin_date = dt_date.fromisoformat(origin_str)
            as_of_dt = datetime(
                origin_date.year, origin_date.month, origin_date.day,
                tzinfo=timezone.utc,
            )

            for target_key in target_keys:
                target_id = key_to_target_id.get(target_key)
                if target_id is None:
                    continue  # already logged above

                entry = {
                    "target_key": target_key,
                    "target_id": target_id,
                    "origin": origin_str,
                    "status": "skipped",
                }

                # Idempotent check
                if run_exists_for_origin(db, target_id, origin_date):
                    entry["reason"] = "existing run within window"
                    summary["results"].append(entry)
                    summary["total_skipped"] += 1
                    logger.info(
                        "SKIP %s @ %s — run already exists",
                        target_key, origin_str,
                    )
                    continue

                if dry_run:
                    entry["status"] = "dry_run_would_create"
                    entry["reason"] = "dry run"
                    summary["results"].append(entry)
                    summary["total_created"] += 1
                    logger.info(
                        "DRY-RUN %s @ %s (horizons=%s)",
                        target_key, origin_str, horizons,
                    )
                    continue

                # ── Compute hindcast ──
                logger.info(
                    "COMPUTE %s @ %s (horizons=%s, as_of=%s)",
                    target_key, origin_str, horizons, origin_str,
                )
                try:
                    run = engine.compute_target(
                        target_id,
                        horizons=horizons,
                        as_of=as_of_dt,
                    )
                    if run is not None:
                        db.commit()
                        entry["status"] = "created"
                        entry["run_id"] = run.id
                        entry["horizon_keys"] = sorted(run.results.keys()) if run.results else []
                        entry["curve_lengths"] = {
                            k: (
                                len(v.get("base", []))
                                if isinstance(v, dict) else
                                "not-a-dict"
                            )
                            for k, v in (run.results or {}).items()
                        }
                        summary["total_created"] += 1
                        logger.info(
                            "OK   %s @ %s → run_id=%s horizons=%s",
                            target_key, origin_str, run.id,
                            entry["horizon_keys"],
                        )
                    else:
                        # compute_target returned None (e.g. insufficient data)
                        # Rollback the current transaction so the session stays clean
                        db.rollback()
                        entry["status"] = "error"
                        entry["reason"] = "compute_target returned None (insufficient data?)"
                        summary["total_errors"] += 1
                        logger.warning(
                            "NULL %s @ %s — compute_target returned None",
                            target_key, origin_str,
                        )
                except Exception as exc:
                    db.rollback()
                    entry["status"] = "error"
                    entry["reason"] = str(exc)[:500]
                    summary["total_errors"] += 1
                    logger.error(
                        "ERR  %s @ %s — %s",
                        target_key, origin_str, exc,
                    )

                summary["results"].append(entry)

    finally:
        db.close()

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Backfill weekly hindcast ForecastRuns for market-dashboard products.",
    )
    parser.add_argument(
        "--origins",
        type=str,
        default=",".join(DEFAULT_ORIGINS),
        help="Comma-separated origin dates YYYY-MM-DD (default: past 4 Mondays)",
    )
    parser.add_argument(
        "--keys",
        type=str,
        default=",".join(PRODUCT_TARGET_KEYS),
        help="Comma-separated ForecastTarget.product_key values",
    )
    parser.add_argument(
        "--horizons",
        type=str,
        default="3,7,15,30",
        help="Comma-separated horizon days (default: 3,7,15,30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be created, do not run the engine",
    )
    args = parser.parse_args()

    origins = parse_origins(args.origins)
    target_keys = parse_origins(args.keys)
    horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]

    logger.info("=" * 60)
    logger.info("Backfill Forecast Runs")
    logger.info("  Origins:      %s", origins)
    logger.info("  Targets:      %d product keys", len(target_keys))
    logger.info("  Horizons:     %s", horizons)
    logger.info("  Dry-run:      %s", args.dry_run)
    logger.info("  Total req:    %d", len(origins) * len(target_keys))
    logger.info("=" * 60)

    summary = backfill(
        origins=origins,
        target_keys=target_keys,
        horizons=horizons,
        dry_run=args.dry_run,
    )

    # ── Print JSON summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print(json.dumps(
        {
            "dry_run": summary["dry_run"],
            "origins": summary["origins"],
            "horizons": summary["horizons"],
            "total_requested": summary["total_requested"],
            "total_created": summary["total_created"],
            "total_skipped": summary["total_skipped"],
            "total_errors": summary["total_errors"],
        },
        indent=2,
        ensure_ascii=False,
    ))

    # Print error details if any
    errors = [r for r in summary["results"] if r["status"] == "error"]
    if errors:
        print("\nERRORS:")
        for e in errors:
            print(f"  {e['target_key']} @ {e['origin']}: {e.get('reason', '?')}")

    if summary["total_errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
