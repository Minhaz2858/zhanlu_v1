"""Backfill cron_expression and next_run_at for existing AutomationTask rows.

Bug: Some tasks were created with a human-readable ``schedule`` (e.g.
"Daily 08:00") but without the derived ``cron_expression`` and
``next_run_at`` columns. The dispatcher only fires tasks where
``next_run_at IS NOT NULL``, so these tasks never run.

This script:
  1. Finds all active tasks where cron_expression IS NULL
     AND next_run_at IS NULL AND schedule IS NOT NULL.
  2. Parses the schedule using the rule-based parser (rules-only, no LLM).
  3. On success: stores cron_expression, computes next_run_at, and
     commits. The next dispatcher tick (within 60s) will fire it.
  4. On failure: leaves the row alone, prints a warning so the user
     can fix the schedule manually.

Safe to run multiple times — it only touches rows with NULL cron_expression.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

# Bootstrap: import the app from the backend dir.
sys.path.insert(0, "/root/zhanlu/backend")

from app.database import SessionLocal  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.schedule_parser import (  # noqa: E402
    next_run_at as _next_run_at,
    safe_parse_schedule_rules_only,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_automation_cron")


def main() -> int:
    db = SessionLocal()
    fixed = 0
    skipped = 0
    try:
        rows = (
            db.query(AutomationTask)
            .filter(
                AutomationTask.is_deleted == False,  # noqa: E712
                AutomationTask.cron_expression.is_(None),
                AutomationTask.next_run_at.is_(None),
            )
            .all()
        )
        log.info("Found %d task(s) with NULL cron_expression", len(rows))
        for task in rows:
            schedule = (task.schedule or "").strip()
            if not schedule or schedule.lower() == "manual":
                log.info(
                    "  [skip] %s '%s' — no schedule (manual)",
                    task.id[:8], (task.name or "")[:40],
                )
                skipped += 1
                continue
            cron = safe_parse_schedule_rules_only(schedule)
            if not cron:
                log.warning(
                    "  [skip] %s '%s' — cannot parse schedule %r",
                    task.id[:8], (task.name or "")[:40], schedule,
                )
                skipped += 1
                continue
            try:
                nxt = _next_run_at(cron, tz_name=task.timezone or "UTC")
            except Exception as e:
                log.warning(
                    "  [skip] %s '%s' — next_run_at compute failed: %s",
                    task.id[:8], (task.name or "")[:40], e,
                )
                skipped += 1
                continue
            task.cron_expression = cron
            task.next_run_at = nxt
            # Also flip to active so the dispatcher will pick it up.
            if task.status == "paused":
                task.status = "active"
            log.info(
                "  [fix]  %s '%s' — schedule=%r → cron=%r, next_run_at=%s",
                task.id[:8], (task.name or "")[:40], schedule, cron, nxt.isoformat(),
            )
            fixed += 1
        db.commit()
        log.info("DONE: %d fixed, %d skipped", fixed, skipped)
    except Exception as e:
        log.exception("Backfill failed: %s", e)
        db.rollback()
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
