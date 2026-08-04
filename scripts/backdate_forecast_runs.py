"""Backdate ForecastRun created_date so the step-function Previous AI line
renders across the past zone in the upstream forecast chart.

For each upstream product (crude_oil, naphtha, cracked_c5):
  - Ensure at least 3 runs exist.
  - If a run was created today, shift its created_date back 13 or 26 days
    so the step-function algorithm spans ~26 days of past history.
  - If fewer than 3 runs, clone the newest run with backdated timestamps.

Run: docker compose exec backend python scripts/backdate_forecast_runs.py
"""
import sys
import logging
from datetime import date, timedelta, datetime
from copy import deepcopy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("backdate_forecast")

from app.database import SessionLocal
from app.models.forecasting import ForecastTarget, ForecastRun

ORG_ID = "default-org"
UPSTREAM_KEYS = {"ecisco.crude_oil", "ecisco.naphtha", "ecisco.cracked_c5"}
BACKDATE_OFFSETS = [13, 26]  # days to shift for 2 additional past runs
TODAY = date.today()


def _ensure_sqlite_datetime(d: date) -> datetime:
    """Return a timezone-naive datetime at midnight for the given date."""
    return datetime(d.year, d.month, d.day, 0, 0, 0)


def main():
    db = SessionLocal()
    try:
        targets = (
            db.query(ForecastTarget)
            .filter(
                ForecastTarget.org_id == ORG_ID,
                ForecastTarget.is_deleted == False,  # noqa: E712
                ForecastTarget.product_key.in_(UPSTREAM_KEYS),
            )
            .all()
        )
        if not targets:
            logger.error("No upstream ForecastTarget rows found. Run seed_and_run_upstream_forecast.py first.")
            return 1

        for tgt in targets:
            runs = (
                db.query(ForecastRun)
                .filter(
                    ForecastRun.target_id == tgt.id,
                    ForecastRun.is_deleted == False,  # noqa: E712
                )
                .order_by(ForecastRun.created_date.desc())
                .all()
            )

            logger.info("Product %s: %d existing run(s)", tgt.product_key, len(runs))

            if not runs:
                logger.warning("  No runs for %s — skipping", tgt.product_key)
                continue

            # We need at least 3 runs: one kept at today, two backdated.
            newest = runs[0]
            # Keep newest as-is (today's forecast)
            logger.info("  Keeping run %s at %s (today)", newest.id, newest.created_date)

            backdate_runs: list[ForecastRun] = []
            if len(runs) >= 3:
                # Backdate the 2nd and 3rd newest
                backdate_runs = [runs[1], runs[2]]
                logger.info("  Found %d runs — backdating runs %s and %s",
                           len(runs), runs[1].id, runs[2].id)
            elif len(runs) == 2:
                # Backdate 2nd + clone newest as 3rd
                backdate_runs.append(runs[1])
                # Clone newest
                clone = _clone_run(db, newest, tgt.id, TODAY - timedelta(days=BACKDATE_OFFSETS[1]))
                if clone:
                    backdate_runs.append(clone)
                    logger.info("  Cloned run %s → %s (id=%s)", newest.id, clone.created_date, clone.id)
            else:
                # Only 1 run — clone 2 more backdated
                for off in BACKDATE_OFFSETS:
                    clone = _clone_run(db, newest, tgt.id, TODAY - timedelta(days=off))
                    if clone:
                        backdate_runs.append(clone)
                        logger.info("  Cloned run %s → %s (id=%s)", newest.id, clone.created_date, clone.id)

            # Apply backdate offsets to the backdated runs
            for i, br in enumerate(backdate_runs):
                target_date = TODAY - timedelta(days=BACKDATE_OFFSETS[i])
                old_created = br.created_date
                br.created_date = _ensure_sqlite_datetime(target_date)
                # Also update as_of_date if present (used by some rendering)
                if br.as_of_date is not None:
                    aod = br.as_of_date
                    # Normalize to date (handle both datetime and date types)
                    if isinstance(aod, datetime):
                        aod_d = aod.date()
                    elif isinstance(aod, date):
                        aod_d = aod
                    else:
                        aod_d = None
                    if aod_d is not None and aod_d >= TODAY:
                        br.as_of_date = target_date
                logger.info("  Backdated run %s: %s → %s", br.id, old_created, br.created_date)

            db.flush()

        db.commit()
        logger.info("===== All backdates committed =====")
        return 0
    except Exception:
        logger.exception("Backdate failed")
        db.rollback()
        return 1
    finally:
        db.close()


def _clone_run(db, source: ForecastRun, target_id: int, new_date: date) -> ForecastRun | None:
    """Create a deep copy of `source` with a different created_date, same results."""
    try:
        # Only copy attributes that exist on the model (be resilient to schema changes)
        safe_kwargs = {
            "target_id": target_id,
            "org_id": source.org_id,
            "app_id": source.app_id,
            "created_date": _ensure_sqlite_datetime(new_date),
            "as_of_date": new_date,
            "results": deepcopy(source.results) if source.results else None,
        }
        # Optional fields — only include if they exist on source
        for attr in ("confidence", "below_naive_baseline", "model_name",
                      "created_by", "ensemble", "metrics", "inputs_summary"):
            if hasattr(source, attr):
                val = getattr(source, attr)
                if isinstance(val, (dict, list)):
                    val = deepcopy(val)
                safe_kwargs[attr] = val

        clone = ForecastRun(**safe_kwargs)
        db.add(clone)
        db.flush()
        return clone
    except Exception:
        logger.exception("Failed to clone run")
        return None


if __name__ == "__main__":
    sys.exit(main())
