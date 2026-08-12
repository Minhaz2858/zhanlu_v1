"""Alert service — detection, persistence and querying of proactive market alerts.

Ported from EDIA 5.1.2 backend/src/service/alert_service.py.
MySQL raw SQL → PostgreSQL via SQLAlchemy ORM.

Detection rules (from EDIA):
- price_spike:  7d change >= +3 %
- price_drop:   7d change <= -3 %
- Severity: high (>= ±6 %), medium (±3-6 %)
- regional_arbitrage: regional spread > freight cost (C5 products)
- 7-day dedup per (product, alert_type); 3-day for arbitrage
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.alert import AlertLog

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────
PRICE_SPIKE_THRESHOLD = 3.0   # +3 % 7d change → price_spike
PRICE_DROP_THRESHOLD = -3.0   # -3 % 7d change → price_drop
HIGH_SEVERITY_THRESHOLD = 6.0   # >= |6| %
DEDUP_WINDOW_DAYS = 7          # price alerts
DEDUP_WINDOW_ARBITRAGE = 3     # arbitrage alerts


class AlertService:
    """CRUD + detection for AlertLog rows."""

    # ── Queries ────────────────────────────────────────────────────────

    @staticmethod
    def get_active_alerts(
        db: Session,
        org_id: str | None = None,
        limit: int = 50,
    ) -> list[AlertLog]:
        """Return non-dismissed alerts, newest first."""
        q = (
            db.query(AlertLog)
            .filter(AlertLog.dismissed_at.is_(None))
            .order_by(AlertLog.triggered_at.desc())
        )
        if org_id:
            q = q.filter(AlertLog.org_id == org_id)
        return q.limit(limit).all()

    @staticmethod
    def count_unread(db: Session, org_id: str | None = None) -> int:
        """Badge count: unread AND non-dismissed."""
        q = db.query(AlertLog).filter(
            AlertLog.read_at.is_(None),
            AlertLog.dismissed_at.is_(None),
        )
        if org_id:
            q = q.filter(AlertLog.org_id == org_id)
        return q.count()

    @staticmethod
    def mark_all_read(db: Session, org_id: str | None = None) -> int:
        """Set read_at = now for all unread, non-dismissed alerts. Returns count."""
        q = db.query(AlertLog).filter(
            AlertLog.read_at.is_(None),
            AlertLog.dismissed_at.is_(None),
        )
        if org_id:
            q = q.filter(AlertLog.org_id == org_id)

        now = datetime.now(timezone.utc)
        count = q.count()
        q.update({"read_at": now}, synchronize_session="fetch")
        db.commit()
        return count

    @staticmethod
    def dismiss_alert(db: Session, alert_id: str, org_id: str | None = None) -> bool:
        """Dismiss a single alert. Returns True if found and updated."""
        q = db.query(AlertLog).filter(AlertLog.id == alert_id)
        if org_id:
            q = q.filter(AlertLog.org_id == org_id)

        alert = q.first()
        if not alert:
            return False

        alert.dismissed_at = datetime.now(timezone.utc)
        db.commit()
        return True

    @staticmethod
    def recent_alert_exists(
        db: Session,
        product_id: str,
        alert_type: str,
        org_id: str | None = None,
        window_days: int = DEDUP_WINDOW_DAYS,
    ) -> bool:
        """Check if an alert already exists within the dedup window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        q = db.query(AlertLog).filter(
            AlertLog.product_id == product_id,
            AlertLog.alert_type == alert_type,
            AlertLog.triggered_at >= cutoff,
        )
        if org_id:
            q = q.filter(AlertLog.org_id == org_id)
        return q.first() is not None

    # ── Detection ──────────────────────────────────────────────────────

    @staticmethod
    def detect_and_persist(
        db: Session,
        org_id: str,
        snapshot: list[dict],
    ) -> list[dict]:
        """Run all detection rules against a perception snapshot, persist new alerts.

        Args:
            db: SQLAlchemy session
            org_id: tenant id
            snapshot: list of product dicts from PerceptionService.build_snapshot().
                      Each dict should have at least:
                      {product_id, product_name, current_price, change_7d_pct, ...}

        Returns:
            list of newly persisted AlertLog.to_dict()
        """
        new_alerts: list[dict] = []

        # ── Price spike / drop detection ────────────────────────────
        for product in snapshot:
            product_id = product.get("product_id")
            product_name = product.get("product_name", product_id)
            change_7d = product.get("change_7d_pct")
            price = product.get("current_price")

            if product_id is None or change_7d is None:
                continue

            # Determine alert type from threshold
            if change_7d >= PRICE_SPIKE_THRESHOLD:
                alert_type = "price_spike"
            elif change_7d <= PRICE_DROP_THRESHOLD:
                alert_type = "price_drop"
            else:
                continue  # within normal range — skip

            # Severity
            severity = (
                "high"
                if abs(change_7d) >= HIGH_SEVERITY_THRESHOLD
                else "medium"
            )

            # Dedup
            if AlertService.recent_alert_exists(
                db, product_id, alert_type, org_id,
                window_days=DEDUP_WINDOW_DAYS,
            ):
                continue

            # Direction word for message
            direction = "surged" if alert_type == "price_spike" else "dropped"
            message = (
                f"{product_name} price {direction} {abs(change_7d):.1f}% "
                f"in 7 days to {price:.2f}" if price is not None
                else f"{product_name} price {direction} {abs(change_7d):.1f}% in 7 days"
            )

            alert = AlertLog(
                org_id=org_id,
                product_id=product_id,
                product_name=product_name,
                alert_type=alert_type,
                severity=severity,
                change_7d_pct=round(change_7d, 2),
                price=round(price, 2) if price is not None else None,
                message=message,
                source="perception",
            )
            db.add(alert)
            new_alerts.append(alert.to_dict())

        # ── Regional arbitrage detection ────────────────────────────
        new_alerts.extend(
            AlertService._detect_arbitrage_alerts(db, org_id, snapshot)
        )

        if new_alerts:
            db.commit()
            logger.info(
                "AlertService: persisted %d new alerts for org=%s",
                len(new_alerts), org_id,
            )

        return new_alerts

    @staticmethod
    def _detect_arbitrage_alerts(
        db: Session, org_id: str, snapshot: list[dict],
    ) -> list[dict]:
        """Detect regional arbitrage: spread > freight cost for C5 products.

        Groups products by product_id, compares regions if FreightCost info available.
        """
        alerts: list[dict] = []

        # Build {product_id: {region: product_entry}}
        product_map: dict[str, dict[str, dict]] = {}
        for p in snapshot:
            pid = p.get("product_id", "")
            region = p.get("region", "")
            if not pid or not region:
                continue
            product_map.setdefault(pid, {})[region] = p

        for pid, regions in product_map.items():
            if len(regions) < 2:
                continue

            region_list = list(regions.items())
            for i in range(len(region_list)):
                for j in range(i + 1, len(region_list)):
                    r1_name, r1 = region_list[i]
                    r2_name, r2 = region_list[j]

                    p1 = r1.get("current_price")
                    p2 = r2.get("current_price")
                    if p1 is None or p2 is None:
                        continue

                    spread = abs(p1 - p2)
                    freight = max(
                        r1.get("freight_cost", 0) or 0,
                        r2.get("freight_cost", 0) or 0,
                    )

                    if spread <= freight:
                        continue

                    # Dedup (3-day window)
                    if AlertService.recent_alert_exists(
                        db, pid, "regional_arbitrage", org_id,
                        window_days=DEDUP_WINDOW_ARBITRAGE,
                    ):
                        continue

                    low_region = r1_name if p1 <= p2 else r2_name
                    high_region = r2_name if p2 >= p1 else r1_name
                    product_name = r1.get("product_name", pid)

                    message = (
                        f"Arbitrage: {product_name} {low_region}→{high_region} "
                        f"spread {spread:.2f} > freight {freight:.2f}"
                    )

                    alert = AlertLog(
                        org_id=org_id,
                        product_id=pid,
                        product_name=product_name,
                        alert_type="regional_arbitrage",
                        severity="medium",
                        price=spread,
                        message=message,
                        source="perception",
                    )
                    db.add(alert)
                    alerts.append(alert.to_dict())

        return alerts
