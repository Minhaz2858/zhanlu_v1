"""Zero-config auto-discovery for new database KnowledgeBases.

P3: When a new database-type KnowledgeBase is created, this module
automatically scans all its tables for forecastable time series and
writes ForecastTarget rows — no manual configuration needed.

Two-layer approach:
 - Layer 1: SQLAlchemy after_insert event listener (fire-and-forget)
 - Layer 2: Explicit API endpoint for manual re-scan
"""

from __future__ import annotations

import logging
import os
import threading

import pandas as pd

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────


def check_auto_discover_enabled() -> bool:
    """Return True when UNIVERSAL_ANALYTICS_AUTO_DISCOVER is true."""
    return os.environ.get(
        "UNIVERSAL_ANALYTICS_AUTO_DISCOVER", "true"
    ).lower() in ("true", "1", "yes")


def _should_discover(kb) -> bool:
    """Determine whether auto-discovery should run for this KB.

    Conditions:
      - source_kind in ("db", "database")
      - db_type is set (non-empty)
      - The feature flag is enabled
    """
    if not check_auto_discover_enabled():
        return False
    if getattr(kb, "source_kind", None) not in ("db", "database"):
        return False
    if not getattr(kb, "db_type", None):
        return False
    return True


def _enqueue_discovery(kb_id: str, org_id: str, app_id: str):
    """Fire-and-forget: spawn a daemon thread to run discovery.

    The thread sleeps briefly to let the INSERT transaction commit,
    then opens a fresh DB session for the discovery work.
    """
    t = threading.Thread(
        target=_run_discovery,
        args=(kb_id, org_id, app_id),
        daemon=True,
    )
    t.start()
    logger.info("Auto-discovery queued for KB %s", kb_id)


# ── Background discovery logic ──────────────────────────────────────


def _run_discovery(
    kb_id: str,
    org_id: str,
    app_id: str,
    sleep_s: float = 2.0,
):
    """Run discovery scan in a fresh session and write ForecastTarget rows.

    This runs in a background thread so it never blocks the HTTP response.

    Args:
        kb_id:     The KnowledgeBase id to scan.
        org_id:    Org that owns the KB.
        app_id:    The app context.
        sleep_s:   Seconds to wait before starting (let INSERT commit).
    """
    import time
    if sleep_s > 0:
        time.sleep(sleep_s)

    from app.deps import SessionLocal
    from app.services.forecasting.discovery import discover

    db = SessionLocal()
    try:
        candidates = discover(db, kb_id)
        written = _write_targets(db, kb_id, org_id, candidates)
        db.commit()
        logger.info(
            "Auto-discovery for KB %s completed: %d targets (%d new)",
            kb_id, len(candidates), written,
        )
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Auto-discovery for KB %s failed: %s", kb_id, exc,
        )
    finally:
        db.close()


def _write_targets(
    db,
    kb_id: str,
    org_id: str,
    candidates: list[dict],
) -> int:
    """Write ForecastTarget rows for discovered candidates.

    Skips duplicates by checking product_key + org_id uniqueness.
    Returns the number of NEW rows written.
    """
    from app.models.forecasting import ForecastTarget

    written = 0
    for c in candidates:
        table = c.get("table", "")
        time_col = c.get("time_column", "")
        measure = c.get("measure", "")
        if not table or not time_col or not measure:
            continue

        product_key = f"discovered-{kb_id[:8]}-{table}-{measure}"

        # Skip duplicates
        existing = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == product_key,
            ForecastTarget.org_id == org_id,
        ).first()
        if existing:
            continue

        target = ForecastTarget(
            name=f"{table}.{measure}",
            product_key=product_key,
            org_id=org_id,
            datasource={
                "source": "generic_kb",
                "kb_id": kb_id,
                "table": table,
                "time_column": time_col,
                "measure": measure,
                "dimensions": c.get("dimensions", []),
            },
            status="discovered",
            source="auto_discovery",
            include_in_weekly_report=False,
        )
        db.add(target)
        written += 1

    return written


# ── SQLAlchemy event listener ───────────────────────────────────────


def register_kb_event_listener():
    """Register the after_insert listener on KnowledgeBase.

    Called at package-import time via universal_analytics/__init__.py.
    """
    try:
        from sqlalchemy import event
        from app.models.knowledge_base import KnowledgeBase

        @event.listens_for(KnowledgeBase, "after_insert")
        def _on_kb_insert(mapper, connection, target):
            if _should_discover(target):
                _enqueue_discovery(
                    kb_id=target.id,
                    org_id=getattr(target, "org_id", "default"),
                    app_id=getattr(target, "app_id", "default"),
                )

        logger.info("Auto-discovery event listener registered on KnowledgeBase")
    except Exception as exc:
        logger.warning(
            "Failed to register auto-discovery event listener: %s", exc,
        )
