"""Scheduled background tasks — periodic memory + skill consolidation.

Runs memory consolidation, skill curation, nightly forecast,
and eval pipelines on a periodic schedule using asyncio background tasks.
Started at app startup.

Schedule:
- Memory consolidation: every 30 minutes per agent
- Skill curation: every 6 hours (global)
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
DAILY_EVAL_INTERVAL = 24 * 60 * 60       # 24 hours (LLM quality eval pipeline)

NIGHTLY_HOUR_UTC = 2  # 2:00 AM UTC

_running_tasks: list[asyncio.Task] = []
_scheduled_failure_count: dict[str, int] = {}


def _inc_failure(task_name: str) -> None:
    """Increment failure counter for observability — prevents silent dead tasks."""
    _scheduled_failure_count[task_name] = _scheduled_failure_count.get(task_name, 0) + 1
    count = _scheduled_failure_count[task_name]
    if count <= 3:
        logger.error("Scheduled task %s failed (occurrence #%d)", task_name, count)
    else:
        logger.warning("Scheduled task %s failed (occurrence #%d)", task_name, count)


async def _run_memory_consolidation_cycle() -> None:
    """Run memory consolidation for all agents with memories.
    
    Delegated to a threadpool via to_thread so the sync ``run_consolidation``
    (which uses ``SessionLocal + db.query``) does not block the event loop.
    """
    await asyncio.to_thread(_memory_consolidation_sync)


def _memory_consolidation_sync() -> None:
    """Sync body of memory consolidation — uses sync SessionLocal."""
    try:
        from app.database import SessionLocal
        from app.models.agent_memory import AgentMemory
        from app.services.memory_manager import run_consolidation
        from sqlalchemy import select, distinct

        db = SessionLocal()
        try:
            result = db.execute(
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
                    _inc_failure("memory_consolidation")
        finally:
            db.close()
    except Exception as e:
        logger.warning("Memory consolidation cycle failed: %s", e)
        _inc_failure("memory_consolidation")


async def _run_skill_curation_cycle() -> None:
    """Run skill curation (report only — no auto-modification).
    
    Delegated to a threadpool via to_thread so the sync ``run_skill_curation``
    (which uses ``SessionLocal + db.query``) does not block the event loop.
    """
    await asyncio.to_thread(_skill_curation_sync)


def _skill_curation_sync() -> None:
    """Sync body of skill curation — uses sync SessionLocal."""
    try:
        from app.database import SessionLocal
        from app.services.skill_curator import run_skill_curation

        db = SessionLocal()
        try:
            report = run_skill_curation(db)
            if report.overlapping_pairs > 0 or report.stale_skills > 0:
                logger.info(
                    "Scheduled skill curation: %d skills, %d overlapping pairs, %d stale",
                    report.total_skills, report.overlapping_pairs, report.stale_skills,
                )
        finally:
            db.close()
    except Exception as e:
        logger.warning("Skill curation cycle failed: %s", e)
        _inc_failure("skill_curation")


# ---- Market Alert Scan (removed with the alerts system) ----------------
# REMOVED with the alerts system (2026-08-27). The market alert scan cycle,
# perception health canary, and CEO morning digest loop no longer exist.


# ---- Daily LLM Quality Eval Pipeline -------------------------------------------

def _run_daily_eval_sync() -> dict:
    """Run the LLM quality evaluation pipeline on recent conversations (sync).

    Gated by EVAL_PIPELINE_ENABLED (default False).
    Returns summary dict: {total, pass_rate, dimensions, ...}.
    """
    from app.database import SessionLocal
    from app.services.eval_pipeline import run_eval_pipeline, build_daily_report

    db = SessionLocal()
    try:
        results = run_eval_pipeline(db)
        report = build_daily_report(results)
        if report["total"] > 0:
            logger.info(
                "[daily-eval] Evaluated %d conversations — pass_rate=%.1f%% — dims=%s",
                report["total"],
                report["pass_rate"] * 100,
                report.get("dimensions", {}),
            )
        return report
    except Exception:
        logger.exception("[daily-eval] Cycle failed")
        return {"error": True}
    finally:
        db.close()


async def _run_daily_eval_cycle() -> None:
    """Run the daily eval pipeline in a thread executor (non-blocking wrapper)."""
    from app.config import settings
    if not getattr(settings, "EVAL_PIPELINE_ENABLED", False):
        return  # silently skip when disabled
    try:
        await asyncio.to_thread(_run_daily_eval_sync)
    except Exception:
        logger.exception("[daily-eval] Unhandled exception")


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


def _run_eval_step(db) -> dict:
    """Evaluation job step — closes the realized_mape loop. Gated."""
    if os.environ.get("FORECAST_EVAL_JOB_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.evaluation_job import run_evaluation
        return run_evaluation(db)
    except Exception:
        logger.exception("[nightly-forecast] eval step failed")
        return {"error": True}


def _run_drift_step(db) -> dict:
    """Drift-response step — detect + audit. Gated (detection needs eval data)."""
    if os.environ.get("FORECAST_DRIFT_AUTO_ADJUST_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.drift_response import check_drift_and_audit
        from app.models.forecasting import ForecastTarget
        targets = db.query(ForecastTarget).filter(
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).all()
        drifting = 0
        for t in targets:
            try:
                if check_drift_and_audit(db, t).get("is_drifting"):
                    drifting += 1
            except Exception:
                logger.exception("[nightly-forecast] drift check failed for %s", t.product_key)
        return {"checked": len(targets), "drifting": drifting}
    except Exception:
        logger.exception("[nightly-forecast] drift step failed")
        return {"error": True}


def _run_event_calibration_step(db) -> dict:
    """T2.2 Event-impact calibration — run event studies for closed events.

    Gated by FORECAST_EVENT_CALIBRATION_ENABLED (default OFF).
    Populates ForecastEventImpact with real price/volume impact data.
    Returns {events_processed, impacts_written, ...} or {skipped: True}.
    """
    if os.environ.get("FORECAST_EVENT_CALIBRATION_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.event_calibration import run_event_calibration
        return run_event_calibration(db, lookback_days=180, window_days=7)
    except Exception:
        logger.exception("[nightly-forecast] event calibration step failed")
        return {"error": True}


def _run_accuracy_feedback_step(db) -> dict:
    """T2.3 Accuracy feedback loop — auto-flag products with degrading accuracy.

    Gated by FORECAST_ACCURACY_FEEDBACK_ENABLED (default OFF).
    Writes ForecastWeightAdjustment audit rows with retrain recommendations.
    Returns {checked, flagged, products} or {skipped: True}.
    """
    if os.environ.get("FORECAST_ACCURACY_FEEDBACK_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.accuracy_feedback import run_accuracy_feedback
        return run_accuracy_feedback(db)
    except Exception:
        logger.exception("[nightly-forecast] accuracy feedback step failed")
        return {"error": True}


def _run_threshold_autotune_step(db) -> dict:
    """T2.4 Threshold auto-tune — stage optimized thresholds from ROI data.

    Gated by FORECAST_THRESHOLD_AUTOTUNE_ENABLED (default OFF).
    Stages results; does NOT auto-apply. Returns {products_checked, staged, ...}.
    """
    if os.environ.get("FORECAST_THRESHOLD_AUTOTUNE_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.threshold_auto_tuner import (
            run_threshold_autotune,
        )
        return run_threshold_autotune(db)
    except Exception:
        logger.exception("[nightly-forecast] threshold autotune step failed")
        return {"error": True}


MAX_REBUILDS_PER_NIGHT = 10


def _run_rebuild_step(db) -> dict:
    """T3.1 Rebuild queue processor — retrain targets flagged needs_rebuild.

    Gated by FORECAST_ACCURACY_FEEDBACK_ENABLED (same flag that sets needs_rebuild).
    Picks up ForecastTarget rows with status='needs_rebuild', retrains them,
    and resets status to 'active' on success.  Caps at MAX_REBUILDS_PER_NIGHT.
    Returns {rebuilt, failed, skipped, total_flagged}.
    """
    from app.config import settings as _settings
    if not getattr(_settings, "FORECAST_ACCURACY_FEEDBACK_ENABLED", False):
        return {"skipped": True}
    try:
        from app.models.forecasting import ForecastTarget
        from app.services.forecasting.engine import ForecastEngine

        targets = (
            db.query(ForecastTarget)
            .filter(
                ForecastTarget.org_id == "default-org",
                ForecastTarget.status == "needs_rebuild",
                ForecastTarget.is_deleted == False,  # noqa: E712
            )
            .limit(MAX_REBUILDS_PER_NIGHT)
            .all()
        )
        if not targets:
            return {"rebuilt": 0, "failed": 0, "skipped": 0, "total_flagged": 0}

        engine = ForecastEngine(db)
        rebuilt = 0
        failed = 0
        for target in targets:
            try:
                engine.compute_target_anchored(target.id)
                target.status = "active"
                # Clear the alert flag so it doesn't get re-flagged immediately
                if target.model_config and "accuracy_alert" in target.model_config:
                    target.model_config["accuracy_alert"]["rebuilt_at"] = datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat()
                db.add(target)
                db.commit()
                rebuilt += 1
                logger.info(
                    "[rebuild] ✓ %s retrained, status→active", target.name
                )
            except Exception:
                db.rollback()
                failed += 1
                logger.exception(
                    "[rebuild] ✗ %s retrain failed, keeping needs_rebuild",
                    target.name,
                )

        total_flagged = db.query(ForecastTarget).filter(
            ForecastTarget.org_id == "default-org",
            ForecastTarget.status == "needs_rebuild",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).count()

        return {
            "rebuilt": rebuilt,
            "failed": failed,
            "skipped": max(0, total_flagged - rebuilt - failed),
            "total_flagged": total_flagged,
        }
    except Exception:
        logger.exception("[nightly-forecast] rebuild step failed")
        return {"error": True}


def _run_realized_price_backfill_step(db) -> dict:
    """T3.2 Realized-price backfill — populate ForecastDecisionLog.actual_price from ERP.

    Runs BEFORE eval so that eval can score against fresh realized prices.
    Gated by FORECAST_DECISION_LOGGING_ENABLED (same flag that creates decision logs).
    Returns {backfilled, skipped, error}.
    """
    from app.config import settings as _settings
    if not getattr(_settings, "FORECAST_DECISION_LOGGING_ENABLED", False):
        return {"skipped": True}
    try:
        from app.services.forecasting.accuracy_tracker import backfill_realized_prices
        result = backfill_realized_prices(db)
        return result
    except Exception:
        logger.exception("[nightly-forecast] realized-price backfill step failed")
        return {"error": True}


def _run_p_rise_calibration_step(db) -> dict:
    """T3.3 p_rise isotonic calibration — weekly recalibration from realized data.

    Gated by FORECAST_P_RISE_CALIBRATION_ENABLED (default OFF).
    Only runs on Mondays (7-day cycle). Persist calibration curves to
    ForecastTarget.model_config["p_rise_calibration"].
    Returns {calibrated, skipped, error}.
    """
    from app.config import settings as _settings
    if not getattr(_settings, "FORECAST_P_RISE_CALIBRATION_ENABLED", False):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.p_rise_calibration import run_weekly_p_rise_calibration
        return run_weekly_p_rise_calibration(db)
    except Exception:
        logger.exception("[nightly-forecast] p_rise calibration step failed")
        return {"error": True}


def _run_champion_challenger_step(db) -> dict:
    """T3.4 Champion/challenger — nightly shadow runs + auto-promotion.

    Gated by FORECAST_CHAMPION_CHALLENGER_ENABLED (default OFF).
    Runs shadow forecasts for registered challengers, persists metrics to DB,
    and auto-promotes when a challenger beats the champion consistently.
    Returns {shadow_runs, promotions, error}.
    """
    from app.config import settings as _settings
    if not getattr(_settings, "FORECAST_CHAMPION_CHALLENGER_ENABLED", False):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.champion_challenger import run_nightly_champion_challenger
        return run_nightly_champion_challenger(db)
    except Exception:
        logger.exception("[nightly-forecast] champion/challenger step failed")
        return {"error": True}


def _run_finetuning_step(db) -> dict:
    """T3.5 Nightly Chronos-Bolt prompt fine-tuning.

    Gated by FORECAST_FINETUNING_ENABLED (default OFF).
    Loads price series, builds pooled dataset, trains soft-prompt tokens,
    and saves them to disk for next forecast run.
    Returns summary dict or {skipped: True}.
    """
    from app.config import settings as _settings
    if not getattr(_settings, "FORECAST_FINETUNING_ENABLED", False):
        return {"skipped": True}
    try:
        from app.services.forecasting.finetuning.finetune_runner import run_nightly_finetuning
        return run_nightly_finetuning(db)
    except Exception:
        logger.exception("[nightly-forecast] finetuning step failed")
        return {"error": True}


def _run_decision_scoring_step(db) -> dict:
    """T2.1 Decision-ROI loop closure — backfill + score pending decisions.

    Gated by FORECAST_DECISION_LOGGING_ENABLED (default OFF).
    Idempotent: only scores logs whose horizon has closed and ROI is not yet
    realised.  Returns {scored_count, ...} or {skipped: True, error: True}.
    """
    if os.environ.get("FORECAST_DECISION_LOGGING_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.decision_loop import run_decision_scoring
        return run_decision_scoring(db)
    except Exception:
        logger.exception("[nightly-forecast] decision scoring step failed")
        return {"error": True}


def _run_nightly_forecast_sync() -> dict:
    """Run the forecast engine for all active targets (sync).

    Returns summary dict: {success, total, failures, elapsed_s}.
    Called via asyncio.to_thread() to avoid blocking the event loop.
    """
    from app.database import SessionLocal
    from app.models.forecasting import ForecastTarget
    from app.services.forecasting.engine import ForecastEngine
    from app.services.forecasting.seed_targets import (
        seed_forecast_targets,
        discover_and_seed_sku_targets,
    )

    db = SessionLocal()
    try:
        # 1. Seed targets (idempotent)
        seeded = seed_forecast_targets(db)
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
                ForecastTarget.status.in_(["active", "discovered"]),
                ForecastTarget.is_deleted == False,  # noqa: E712
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
        backfill_used = 0
        t0 = time.monotonic()
        for target in targets:
            target_t0 = time.monotonic()
            try:
                engine.compute_target_anchored(target.id)
                db.commit()  # Persist the ForecastRun (engine only flushes)
                elapsed = time.monotonic() - target_t0
                success_count += 1
                logger.info(
                    "[nightly-forecast] ✓ %s (%.1fs)",
                    target.name,
                    elapsed,
                )
            except Exception as exc:
                db.rollback()  # Roll back failed target, keep prior commits
                elapsed = time.monotonic() - target_t0
                logger.warning(
                    "[nightly-forecast] ✗ %s primary engine failed (%.1fs): %s — falling back to backfill",
                    target.name, elapsed, exc,
                )
                # Fallback: simple backfill that always produces a valid
                # ForecastRun (with explanation.probability populated) so the
                # decision board does not fall back to "数据不足,建议观望".
                try:
                    from app.services.forecasting.backfill_forecast import backfill_target
                    run = backfill_target(target, db)
                    if run is not None:
                        db.commit()
                        backfill_used += 1
                        logger.info(
                            "[nightly-forecast] ✓ %s backfilled (%.1fs total)",
                            target.name, time.monotonic() - target_t0,
                        )
                        continue
                except Exception as bf_exc:
                    logger.error(
                        "[nightly-forecast] backfill also failed for %s: %s",
                        target.name, bf_exc,
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
        _eval_on = os.environ.get("FORECAST_EVAL_JOB_ENABLED", "false").lower() in ("1", "true", "yes")
        _drift_on = os.environ.get("FORECAST_DRIFT_AUTO_ADJUST_ENABLED", "false").lower() in ("1", "true", "yes")
        _decision_on = os.environ.get("FORECAST_DECISION_LOGGING_ENABLED", "false").lower() in ("1", "true", "yes")
        _event_cal_on = os.environ.get("FORECAST_EVENT_CALIBRATION_ENABLED", "false").lower() in ("1", "true", "yes")
        _acc_fb_on = os.environ.get("FORECAST_ACCURACY_FEEDBACK_ENABLED", "false").lower() in ("1", "true", "yes")
        _autotune_on = os.environ.get("FORECAST_THRESHOLD_AUTOTUNE_ENABLED", "false").lower() in ("1", "true", "yes")
        _p_rise_cal_on = os.environ.get("FORECAST_P_RISE_CALIBRATION_ENABLED", "false").lower() in ("1", "true", "yes")
        _champ_on = os.environ.get("FORECAST_CHAMPION_CHALLENGER_ENABLED", "false").lower() in ("1", "true", "yes")
        # Fallback to pydantic settings for flags that may only be in .env
        if not _p_rise_cal_on:
            try:
                from app.config import settings as _s
                _p_rise_cal_on = getattr(_s, "FORECAST_P_RISE_CALIBRATION_ENABLED", False)
            except Exception:
                pass
        if not _champ_on:
            try:
                from app.config import settings as _s
                _champ_on = getattr(_s, "FORECAST_CHAMPION_CHALLENGER_ENABLED", False)
            except Exception:
                pass

        # Step order matters: realized-price backfill FIRST so eval has fresh data
        summary["realized_price_backfill"] = _run_realized_price_backfill_step(db) if _decision_on else {"skipped": True}
        summary["eval"] = _run_eval_step(db) if _eval_on else {"skipped": True}
        summary["drift"] = _run_drift_step(db) if _drift_on else {"skipped": True}
        summary["decision_scoring"] = _run_decision_scoring_step(db) if _decision_on else {"skipped": True}
        summary["event_calibration"] = _run_event_calibration_step(db) if _event_cal_on else {"skipped": True}
        summary["accuracy_feedback"] = _run_accuracy_feedback_step(db) if _acc_fb_on else {"skipped": True}
        summary["rebuild"] = _run_rebuild_step(db) if _acc_fb_on else {"skipped": True}
        summary["p_rise_calibration"] = _run_p_rise_calibration_step(db) if _p_rise_cal_on else {"skipped": True}
        summary["champion_challenger"] = _run_champion_challenger_step(db) if _champ_on else {"skipped": True}
        summary["finetuning"] = _run_finetuning_step(db)
        summary["threshold_autotune"] = _run_threshold_autotune_step(db) if _autotune_on else {"skipped": True}
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


# ---- Digest loops ------------------------------------------------------
# REMOVED with the alerts system (2026-08-27): CEO morning digest and the
# weekly summary/forecast digest loops no longer exist.


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
    Uses a Postgres advisory lock to prevent duplicate startup when
    running with multiple uvicorn workers — only ONE worker wins.
    """
    global _running_tasks

    # Don't start twice (in-process guard)
    if _running_tasks:
        logger.warning("Scheduled tasks already running — skipping")
        return

    # ── Multi-worker guard: Postgres advisory lock ──
    if not _try_acquire_scheduler_lock():
        logger.info(
            "Scheduled tasks NOT started — another worker holds the scheduler lock. "
            "This is expected in multi-worker deployments."
        )
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
            _nightly_forecast_loop(),
            name="sched-nightly-forecast",
        ),
        loop.create_task(
            _periodic_task("daily_eval", DAILY_EVAL_INTERVAL, _run_daily_eval_cycle),
            name="sched-daily-eval",
        ),
    ]

    for task in tasks:
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    _running_tasks = tasks
    logger.info("Started %d scheduled background tasks", len(tasks))


def _try_acquire_scheduler_lock() -> bool:
    """Acquire a Postgres advisory lock for the scheduler.

    Only one uvicorn worker (or process) should run background tasks.
    Returns True if the lock was acquired, False otherwise.
    Falls back to an env-var gate when Postgres is unavailable.
    """
    # Env-var shortcut: SCHEDULER_WORKER_ID=0 always wins
    worker_id = os.environ.get("SCHEDULER_WORKER_ID")
    if worker_id is not None:
        return worker_id == "0"

    try:
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            # pg_try_advisory_lock(380630) — arbitrary unique bigint key
            result = db.execute("SELECT pg_try_advisory_lock(380630)").scalar()
            db.commit()
            if result:
                logger.info("Scheduler advisory lock acquired (pg_try_advisory_lock)")
            return bool(result)
        finally:
            db.close()
    except Exception:
        # Non-Postgres fallback (SQLite, etc.): allow startup
        logger.warning(
            "Could not acquire scheduler advisory lock (database may not support it). "
            "Starting scheduled tasks anyway — if running multiple workers, tasks may duplicate."
        )
        return True


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
    "DAILY_EVAL_INTERVAL",
    "_calculate_seconds_until_2am",
    "_run_nightly_forecast_cycle",
    "_run_nightly_forecast_sync",
    "_run_daily_eval_cycle",
]
