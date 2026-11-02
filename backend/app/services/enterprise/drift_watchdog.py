"""Scheduled ontology drift monitoring — lifespan watchdog (Phase 5C).

Turns Phase 4's manual drift/re-onboarding into a continuous process
WITHOUT a new scheduling platform: this module mirrors the repository's
existing lifespan-watchdog pattern (``catalog_watchdog`` — asyncio task
started from ``main.py`` lifespan, stopped via an ``asyncio.Event``) and
reuses the Phase 4 onboarding pipeline — there is NO second discovery
engine here.

Per cycle (``scan_once``):
1. Candidates = the distinct (org_id, resource_type, resource_id) triples
   that have an onboarding run (the watchdog never invents scope — only
   resources someone onboarded are monitored).
2. For each candidate (bounded by MAX_RESOURCES_PER_CYCLE): compute the
   CURRENT inventory digest; a resource type with no digest capability is
   skipped HONESTLY (no fake drift); a digest differing from the latest
   run's triggers ``on_resource_changed`` (which re-onboards with
   retry=drifted, records stale references, never deletes evidence).
3. Errors are isolated per resource: one failing connector never stops
   the others; failures back off for RETRY_BACKOFF_SECONDS.
4. In-flight guard: a resource already being onboarded (concurrent cycle
   or a manual call in this process) is skipped, never double-processed.

Shutdown: ``run_drift_watchdog`` exits cleanly when the stop event is set
— no untracked background workers survive the lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import SessionLocal
from app.services.enterprise.onboarding import (
    _resource_exists, on_resource_changed)

logger = logging.getLogger(__name__)
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

# In-process concurrency guards (single-process lifespan pattern, same
# scope as every other watchdog in this repository).
_IN_FLIGHT: set = set()
_IN_FLIGHT_LOCK = threading.Lock()

# key -> {"count": int, "not_before": float (monotonic), "error": str}
_FAILURES: dict = {}

# Cycle bookkeeping surfaced by get_drift_status().
_LAST_CYCLE: dict = {}


def try_mark_in_flight(key: tuple) -> bool:
    """Atomically claim a resource key. False = already in flight."""
    with _IN_FLIGHT_LOCK:
        if key in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(key)
        return True


def release_in_flight(key: tuple) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard(key)


def _in_backoff(key: tuple, now: float) -> bool:
    f = _FAILURES.get(key)
    return bool(f and now < f.get("not_before", 0))


def _record_failure(key: tuple, error: str) -> None:
    backoff = int(getattr(
        settings, "ONTOLOGY_DRIFT_WATCHDOG_RETRY_BACKOFF_SECONDS", 900))
    prev = _FAILURES.get(key, {}).get("count", 0)
    _FAILURES[key] = {
        "count": prev + 1,
        "not_before": time.monotonic() + backoff,
        "error": str(error)[:200],
    }


def _clear_failure(key: tuple) -> None:
    _FAILURES.pop(key, None)


def clear_failure_state_for_tests() -> None:
    """Test isolation helper: reset the in-process failure/backoff map."""
    _FAILURES.clear()


def scan_once(db=None) -> dict:
    """One bounded scan cycle. Returns a summary; never raises (errors are
    per-resource and recorded in the summary/backoff map)."""
    from app.models.enterprise_onboarding import EnterpriseOnboardingRun
    from app.services.enterprise.onboarding import (
        _latest_run,
        _resource_digest,
    )

    own = db is None
    if own:
        db = SessionLocal()
    now = time.monotonic()
    summary = {"scanned": 0, "drifted": 0, "unchanged": 0, "failed": 0,
               "skipped_in_flight": 0, "skipped_backoff": 0,
               "skipped_no_digest": 0, "skipped_missing": 0, "errors": []}
    try:
        max_resources = int(getattr(
            settings, "ONTOLOGY_DRIFT_WATCHDOG_MAX_RESOURCES_PER_CYCLE", 20))
        candidates = (
            db.query(
                EnterpriseOnboardingRun.org_id,
                EnterpriseOnboardingRun.resource_type,
                EnterpriseOnboardingRun.resource_id,
            )
            .filter(EnterpriseOnboardingRun.is_deleted == False)  # noqa: E712
            .distinct()
            .all()
        )
        for org_id, rtype, rid in candidates:
            if summary["scanned"] >= max_resources:
                break
            key = (org_id, rtype, rid)
            if not try_mark_in_flight(key):
                summary["skipped_in_flight"] += 1
                continue
            try:
                if _in_backoff(key, now):
                    summary["skipped_backoff"] += 1
                    continue
                # per-candidate session: an error in one candidate must
                # never poison another candidate's transaction state
                rdb = SessionLocal()
                try:
                    # Phase 7C (D1): existence FIRST. A deleted resource
                    # must not produce a failed run row every cycle — the
                    # candidate is tombstoned (run rows soft-deleted,
                    # org-scoped) so it leaves the candidate set for good.
                    if not _resource_exists(rdb, org_id, rtype, rid):
                        summary["skipped_missing"] += 1
                        from app.models.enterprise_onboarding import (
                            EnterpriseOnboardingRun)
                        tombstoned = (
                            rdb.query(EnterpriseOnboardingRun)
                            .filter(
                                EnterpriseOnboardingRun.org_id == org_id,
                                EnterpriseOnboardingRun.resource_type == rtype,
                                EnterpriseOnboardingRun.resource_id == rid,
                                EnterpriseOnboardingRun.is_deleted == False,  # noqa: E712
                            )
                            .all()
                        )
                        for run_row in tombstoned:
                            run_row.is_deleted = True
                        if tombstoned:
                            rdb.commit()
                            logger.info(
                                "drift_watchdog: tombstoned %s:%s (org=%s) "
                                "(%d run rows) — resource no longer exists",
                                rtype, rid, org_id, len(tombstoned))
                        continue
                    current = _resource_digest(rdb, org_id, rtype, rid)
                    if current is None:
                        # honest: this resource type has no digest
                        # capability — drift monitoring does not apply
                        summary["skipped_no_digest"] += 1
                        continue
                    last = _latest_run(rdb, org_id, rtype, rid)
                    last_digest = (last.inventory_digest
                                   if last is not None else None)
                    summary["scanned"] += 1
                    if last_digest == current:
                        summary["unchanged"] += 1
                        continue
                    result = on_resource_changed(rdb, org_id, rtype, rid,
                                                 actor_id="drift-watchdog")
                    if result.get("drift_detected"):
                        summary["drifted"] += 1
                        logger.info(
                            "drift_watchdog: re-onboarded %s:%s (org=%s) "
                            "status=%s", rtype, rid, org_id,
                            (result.get("run") or {}).get("status"))
                    else:
                        summary["unchanged"] += 1
                    _clear_failure(key)
                finally:
                    rdb.close()
            except Exception as e:  # noqa: BLE001 — per-resource isolation
                summary["failed"] += 1
                summary["errors"].append(
                    {"resource_type": rtype, "resource_id": rid,
                     "org_id": org_id, "error": str(e)[:200]})
                _record_failure(key, e)
                logger.warning("drift_watchdog: %s:%s (org=%s) failed: %s",
                               rtype, rid, org_id, e)
            finally:
                release_in_flight(key)
        return summary
    finally:
        if own:
            db.close()
        _stamp_cycle(summary)


def _stamp_cycle(summary: dict) -> None:
    """Record cycle bookkeeping (informational surface for get_drift_status)."""
    try:
        interval = int(getattr(
            settings, "ONTOLOGY_DRIFT_WATCHDOG_INTERVAL_SECONDS", 3600))
        now = datetime.now(timezone.utc)
        _LAST_CYCLE.clear()
        _LAST_CYCLE.update(summary)
        _LAST_CYCLE.update({
            "last_run": now.isoformat(),
            "next_run": (now + timedelta(seconds=interval)).isoformat(),
            "interval_seconds": interval,
        })
    except Exception:  # noqa: BLE001 — status is never load-bearing
        logger.debug("drift_watchdog: cycle bookkeeping failed", exc_info=True)


def get_drift_status() -> dict:
    """Watchdog cycle status: last run, next run, and the drift/failure
    counters of the most recent cycle (empty dict before the first scan —
    missing stays missing)."""
    return dict(_LAST_CYCLE)


async def run_drift_watchdog(stop_event: asyncio.Event) -> None:
    """Run the watchdog loop until ``stop_event`` is set.

    Should be launched as ``asyncio.create_task(run_drift_watchdog(...))``
    from the FastAPI lifespan (same pattern as catalog_watchdog). When
    disabled it does NOTHING — not even one scan.
    """
    interval = int(getattr(
        settings, "ONTOLOGY_DRIFT_WATCHDOG_INTERVAL_SECONDS", 3600))
    if not getattr(settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", False):
        logger.info("drift_watchdog: disabled (flag OFF) — not started")
        return
    logger.info("drift_watchdog: starting (interval=%ds)", interval)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(scan_once)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.exception("drift_watchdog: scan iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # stop requested
        except asyncio.TimeoutError:
            continue
    logger.info("drift_watchdog: stopped")
