"""Scheduled background tasks — periodic memory + skill consolidation.

Runs memory consolidation and skill curation on a periodic schedule using
asyncio background tasks. Started at app startup, runs fire-and-forget.

Schedule:
- Memory consolidation: every 30 minutes per agent
- Skill curation: every 6 hours (global)
- Alert check: every 5 minutes

All tasks are non-fatal — failures are logged but never crash the app.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Schedule intervals (seconds)
MEMORY_CONSOLIDATION_INTERVAL = 30 * 60  # 30 minutes
SKILL_CURATION_INTERVAL = 6 * 60 * 60   # 6 hours
ALERT_CHECK_INTERVAL = 5 * 60            # 5 minutes
NEWS_INGESTION_INTERVAL = 60 * 60        # 1 hour (intelligence layer)

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
]
