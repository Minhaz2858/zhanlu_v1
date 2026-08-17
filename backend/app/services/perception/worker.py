"""Background worker that periodically scans for new market alerts.

Ported from EDIA 5.1.2 PerceptionWorker (threading.Thread).
Adapted to Zhanlu's async FastAPI — runs via asyncio.to_thread + SessionLocal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import settings
from app.core.mysql_db import is_edia_reachable
from app.services.perception.service import build_perception_snapshot
from app.services.perception.alert_service import AlertService

logger = logging.getLogger(__name__)


class _DefaultPref:
    """Quietest-safe default preferences for the org-wide scan.

    Replaced by per-user AlertPrefSnapshot.from_user(...) once the
    preferences router (Task 5) is in place.
    """

    muted_products: list[str] = []
    min_severity: str = "low"
    threshold_multiplier: float = 1.0
    recommendations_enabled: bool = True


def _default_pref_snapshot() -> _DefaultPref:
    return _DefaultPref()


async def run_alert_scan_cycle() -> dict:
    """One scan cycle: snapshot → detect → persist.

    Returns dict with {scanned_at, new_alerts, total_scanned, error, reachable, reason}.

    Called by the periodic scheduled task in scheduled_tasks.py,
    gated by ``ECISCO_BI_ALERTS_ENABLED``.
    """
    if not settings.ECISCO_BI_ALERTS_ENABLED:
        return {"skipped": True, "reason": "flag disabled"}

    result: dict = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "new_alerts": 0,
        "total_scanned": 0,
        "error": None,
        "reachable": None,
        "reason": None,
    }

    # Reachability probe up-front so the UI can surface "warehouse unreachable"
    # even when no products scan (the most common silent-zero cause).
    result["reachable"] = is_edia_reachable()
    if not result["reachable"]:
        logger.warning(
            "AlertScannerWorker: EDIA warehouse unreachable — 0 alerts will be "
            "generated. Check EDIA_MYSQL_URL and network reachability to "
            "10.10.10.49:3306."
        )
        result["reason"] = "warehouse unreachable"
        return result

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        # 1. Build snapshot WITH db session so _get_product_price can query
        snapshot = build_perception_snapshot(db=db)

        # 2. Extract products dict from snapshot, map to list with correct keys
        #    (build_perception_snapshot returns {products: {id: {...}}, ...} but
        #    detect_and_persist expects list[dict] with keys: product_id, current_price, ...)
        products_raw = snapshot.get("products", {})
        product_list = []
        for v in products_raw.values():
            if v.get("latest_price") is None:
                continue
            product_list.append({
                "product_id": v["id"],
                "product_name": v.get("name_cn") or v.get("name_en") or v["id"],
                "current_price": v["latest_price"],
                "change_7d_pct": v.get("change_7d_pct", 0.0),
                "region": v.get("region"),
                "freight_cost": v.get("freight_cost"),
            })

        result["total_scanned"] = len(product_list)

        if not product_list:
            logger.warning(
                "AlertScannerWorker: snapshot returned 0 products with prices — "
                "nothing to scan. Check perception._get_product_price / "
                "market_dashboard_service.read_product_history_rows."
            )
            return result

        # 3. Detect and persist (Smart Alerts mode when enabled)
        org_id = settings.DEFAULT_ORG_ID
        if settings.SMART_ALERTS_ENABLED:
            from app.services.perception.smart_evaluator import SmartEvaluator

            pref = _default_pref_snapshot()
            evaluator = SmartEvaluator(db=db)
            product_list = evaluator.evaluate(product_list, pref)

        new_alerts = AlertService.detect_and_persist(db, org_id, product_list)
        result["new_alerts"] = len(new_alerts)

        if new_alerts:
            logger.info(
                "AlertScannerWorker: persisted %d new alerts (scanned %d products)",
                len(new_alerts), result["total_scanned"],
            )

    except Exception as e:
        logger.exception("AlertScannerWorker: scan failed — %s", e)
        result["error"] = str(e)
    finally:
        db.close()

    return result
