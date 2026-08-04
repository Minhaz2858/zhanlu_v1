"""Scheduled background tasks — periodic memory + skill consolidation.

Runs memory consolidation, skill curation, nightly forecast, news ingestion,
and alert checks on a periodic schedule using asyncio background tasks.
Started at app startup.

Schedule:
- Memory consolidation: every 30 minutes per agent
- Skill curation: every 6 hours (global)
- Alert check: every 5 minutes
- News ingestion: every 1 hour (intelligence layer)
- Nightly forecast: daily at 2:00 AM UTC

All tasks are non-fatal — failures are logged but never crash the app.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Schedule intervals (seconds)
MEMORY_CONSOLIDATION_INTERVAL = 30 * 60  # 30 minutes
SKILL_CURATION_INTERVAL = 6 * 60 * 60   # 6 hours
ALERT_CHECK_INTERVAL = 5 * 60            # 5 minutes
NEWS_INGESTION_INTERVAL = 60 * 60        # 1 hour (intelligence layer)

NIGHTLY_HOUR_UTC = 2  # 2:00 AM UTC

_running_tasks: list[asyncio.Task] = []


async def _run_memory_consolidation_cycle() -> None:
    """Run memory consolidation for all agents with memories."""
    try:
        from app.database import AsyncSessionLocal
        from app.models.agent_app import AgentApp
        from app.services.memory_manager import run_consolidation
        from sqlalchemy import select, distinct

        async with AsyncSessionLocal() as db:
            # Get distinct agent_app_ids that have memories
            from app.models.agent_memory import AgentMemory
            result = await db.execute(
                select(distinct(AgentMemory.agent_app_id)).where(
                    AgentMemory.is_deleted == False
                )
            )
            agent_ids = [row[0] for row in result.fetchall() if row[0]]

            for agent_id in agent_ids:
                try:
                    report = run_consolidation(db, agent_id)
                    if report.total_before != report.total_after:
                        logger.info(
                            "Scheduled memory consolidation for %s: %d -> %d (merged=%d, expired=%d, archived=%d)",
                            agent_id, report.total_before, report.total_after,
                            report.semantic_duplicates_merged, report.expired_removed, report.stale_archived,
                        )
                except Exception as e:
                    logger.warning("Memory consolidation failed for %s: %s", agent_id, e)
    except Exception as e:
        logger.warning("Memory consolidation cycle failed: %s", e)


async def _run_skill_curation_cycle() -> None:
    """Run skill curation (report only — no auto-modification)."""
    try:
        from app.database import AsyncSessionLocal
        from app.services.skill_curator import run_skill_curation

        async with AsyncSessionLocal() as db:
            report = run_skill_curation(db)
            if report.overlapping_pairs > 0 or report.stale_skills > 0:
                logger.info(
                    "Scheduled skill curation: %d skills, %d overlapping pairs, %d stale",
                    report.total_skills, report.overlapping_pairs, report.stale_skills,
                )
    except Exception as e:
        logger.warning("Skill curation cycle failed: %s", e)


async def _run_alert_check_cycle() -> None:
    """Run alert threshold checks."""
    try:
        from app.services.alerting import check_alerts
        alerts = check_alerts()
        for alert in alerts:
            logger.warning("ALERT: %s", alert)
    except Exception as e:
        logger.debug("Alert check failed: %s", e)


async def _run_news_ingestion_cycle() -> None:
    """Run one news ingestion scan cycle for the intelligence layer."""
    try:
        from app.database import AsyncSessionLocal
        from app.services.intelligence.news_ingestion_agent import NewsIngestionAgent

        async with AsyncSessionLocal() as db:
            agent = NewsIngestionAgent(db, org_id="default-org")
            await agent.run_news_cycle(trigger="scheduled")
            await db.commit()
    except Exception as e:
        logger.warning("News ingestion cycle failed: %s", e)


# ---- Nightly Forecast (2 AM UTC daily, NOT a UI-visible AutomationTask) -------

def _calculate_seconds_until_2am() -> float:
    """Return seconds from now until the next 02:00 UTC occurrence.

    If the current time is exactly 02:00:00 UTC, returns 86400 (next day).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=NIGHTLY_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def _run_nightly_forecast_sync() -> dict:
    """Run the forecast engine for all active Ecisco BI targets (sync).

    Returns summary dict: {success, total, failures, elapsed_s}.
    Called via asyncio.to_thread() to avoid blocking the event loop.
    """
    from app.database import SessionLocal
    from app.models.forecasting import ForecastTarget
    from app.services.forecasting.engine import ForecastEngine
    from app.services.forecasting.seed_ecisco_targets import (
        seed_ecisco_forecast_targets,
        discover_and_seed_sku_targets,
    )

    db = SessionLocal()
    try:
        # 1. Seed targets (idempotent)
        seeded = seed_ecisco_forecast_targets(db)
        # 1b. Discover and seed ERP SKU targets (idempotent)
        sku_seeded = discover_and_seed_sku_targets(db)
        logger.info(
            "[nightly-forecast] Seed complete — %d targets available",
            seeded,
        )

        # 2. Fetch all active targets for the default org
        targets = (
            db.query(ForecastTarget)
            .filter(
                ForecastTarget.org_id == "default-org",
                ForecastTarget.is_active == True,  # noqa: E712
            )
            .all()
        )

        if not targets:
            logger.warning("[nightly-forecast] No active targets found")
            return {"success": 0, "total": 0, "failures": [], "elapsed_s": 0.0}

        # 3. Run forecast for each target (sequentially for safety)
        engine = ForecastEngine(db)
        success_count = 0
        failures = []
        t0 = time.monotonic()
        for target in targets:
            target_t0 = time.monotonic()
            try:
                engine.compute_target_anchored(target.id)
                elapsed = time.monotonic() - target_t0
                success_count += 1
                logger.info(
                    "[nightly-forecast] ✓ %s (%.1fs)",
                    target.name,
                    elapsed,
                )
            except Exception:
                elapsed = time.monotonic() - target_t0
                logger.exception(
                    "[nightly-forecast] ✗ %s failed (%.1fs)",
                    target.name,
                    elapsed,
                )
                failures.append(target.name)

        total_elapsed = time.monotonic() - t0
        summary = {
            "success": success_count,
            "total": len(targets),
            "failures": failures,
            "elapsed_s": total_elapsed,
        }
        logger.info(
            "[nightly-forecast] DONE — %d/%d succeeded in %.1fs%s",
            success_count,
            len(targets),
            total_elapsed,
            f"; failures: {failures}" if failures else "",
        )
        return summary
    finally:
        db.close()


async def _run_nightly_forecast_cycle() -> None:
    """Run the nightly forecast in a thread executor (non-blocking wrapper)."""
    enabled = os.environ.get("NIGHTLY_FORECAST_ENABLED", "true").lower() not in (
        "0", "false", "no",
    )
    if not enabled:
        logger.info("[nightly-forecast] DISABLED by NIGHTLY_FORECAST_ENABLED=False")
        return

    try:
        await asyncio.to_thread(_run_nightly_forecast_sync)
    except Exception:
        logger.exception("[nightly-forecast] Unhandled exception")


async def _nightly_forecast_loop() -> None:
    """Infinite loop: sleep until next 2 AM UTC, run cycle, repeat."""
    while True:
        delay = _calculate_seconds_until_2am()
        next_run = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=delay
        )
        logger.info(
            "[nightly-forecast] Next run in %.1f hours (%s UTC)",
            delay / 3600,
            next_run.isoformat(),
        )
        await asyncio.sleep(delay)
        try:
            await _run_nightly_forecast_cycle()
        except Exception:
            logger.exception(
                "[nightly-forecast] Unhandled exception in nightly loop"
            )
        # Brief cooldown to prevent tight retry on persistent errors
        await asyncio.sleep(5)


async def _periodic_task(
    name: str,
    interval: int,
    fn: Any,
) -> None:
    """Run a function periodically with error isolation."""
    logger.info("Started scheduled task: %s (interval=%ds)", name, interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await fn()
        except asyncio.CancelledError:
            logger.info("Scheduled task %s cancelled", name)
            raise
        except Exception as e:
            logger.warning("Scheduled task %s error: %s", name, e)
            # Continue running even after errors


def start_scheduled_tasks() -> None:
    """Start all scheduled background tasks.

    Call this once at app startup. Tasks run fire-and-forget.
    """
    global _running_tasks

    # Don't start twice
    if _running_tasks:
        logger.warning("Scheduled tasks already running — skipping")
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("No event loop — scheduled tasks not started")
        return

    tasks = [
        loop.create_task(
            _periodic_task("memory_consolidation", MEMORY_CONSOLIDATION_INTERVAL, _run_memory_consolidation_cycle),
            name="sched-memory-consolidation",
        ),
        loop.create_task(
            _periodic_task("skill_curation", SKILL_CURATION_INTERVAL, _run_skill_curation_cycle),
            name="sched-skill-curation",
        ),
        loop.create_task(
            _periodic_task("alert_check", ALERT_CHECK_INTERVAL, _run_alert_check_cycle),
            name="sched-alert-check",
        ),
        loop.create_task(
            _periodic_task("news_ingestion", NEWS_INGESTION_INTERVAL, _run_news_ingestion_cycle),
            name="sched-news-ingestion",
        ),
        loop.create_task(
            _nightly_forecast_loop(),
            name="sched-nightly-forecast",
        ),
    ]

    for task in tasks:
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    _running_tasks = tasks
    logger.info("Started %d scheduled background tasks", len(tasks))


async def stop_scheduled_tasks() -> None:
    """Stop all scheduled background tasks (for graceful shutdown)."""
    global _running_tasks
    for task in _running_tasks:
        task.cancel()
    if _running_tasks:
        await asyncio.gather(*_running_tasks, return_exceptions=True)
    _running_tasks = []
    logger.info("Stopped all scheduled background tasks")


__all__ = [
    "start_scheduled_tasks",
    "stop_scheduled_tasks",
    "MEMORY_CONSOLIDATION_INTERVAL",
    "SKILL_CURATION_INTERVAL",
    "ALERT_CHECK_INTERVAL",
    "NEWS_INGESTION_INTERVAL",
    "_calculate_seconds_until_2am",
    "_run_nightly_forecast_cycle",
    "_run_nightly_forecast_sync",
]
