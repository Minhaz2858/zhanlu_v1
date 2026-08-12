"""CEO Morning Digest — core service.

Collects recent alerts, computes ¥ impact, ranks by margin at stake,
generates a narrative (LLM or template), and persists as a single
AlertLog row with ``alert_type="daily_digest"``.

Gated by ``ALERT_DIGEST_ENABLED`` (default False).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alert import AlertLog

logger = logging.getLogger(__name__)


class DigestService:
    """Build and persist the CEO morning digest."""

    def __init__(self, db: Session, org_id: str | None = None):
        self.db = db
        self.org_id = org_id

    # ── Main entry ──────────────────────────────────────────────────────

    async def build_and_persist(self) -> AlertLog | None:
        """Collect → compute → rank → narrate → persist.

        Returns None if no items (quiet day) or today's digest already exists.
        """
        items = await self.collect()
        if not items:
            logger.info("DigestService: no alerts in 24h window — quiet day")
            return None

        # Compute ¥ impact for each item
        for item in items:
            impact = self.compute_impact(item)
            item.update(impact)

        # Rank by ¥ impact
        ranked = self.rank(items)

        # Dedup: check if today's digest already exists
        if self._today_digest_exists():
            logger.info("DigestService: today's digest already exists — skipping")
            return None

        # Narrate or template
        from app.services.perception.digest_narrator import DigestNarrator

        narrator = DigestNarrator()
        narrative = await narrator.narrate(ranked, date_str=self._today_str())
        if narrative is None:
            narrative = self.render_template(ranked)
            narrator_used = "template"
        else:
            narrator_used = "llm"

        # Persist
        return self._persist(ranked, narrative, narrator_used)

    # ── Collect ─────────────────────────────────────────────────────────

    async def collect(self, window_hours: int = 24) -> list[dict]:
        """Gather alerts from last 24h from alert_logs.

        Only includes price_spike, price_drop, and regional_arbitrage alerts
        that haven't been dismissed.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        query = (
            self.db.query(AlertLog)
            .filter(
                AlertLog.alert_type.in_(["price_spike", "price_drop", "regional_arbitrage"]),
                AlertLog.triggered_at >= cutoff,
                AlertLog.dismissed_at.is_(None),
            )
        )
        if self.org_id:
            query = query.filter(AlertLog.org_id == self.org_id)

        alerts = query.order_by(AlertLog.triggered_at.desc()).all()
        return [self._alert_to_dict(a) for a in alerts]

    # ── Impact computation ──────────────────────────────────────────────

    def compute_impact(self, alert: dict) -> dict:
        """Compute ¥ impact per day for a single alert.

        Price alerts:  |price × change_7d_pct%| ÷ 7 × avg_daily_volume_tons
        Arbitrage:     (spread − freight) × avg_daily_volume_tons

        Volume source: erp_product_sales_details.shipment_quantity,
                       30d trailing avg, FNAME match.
        """
        avg_volume = self._get_avg_daily_volume(alert.get("product_name", ""))
        alert_type = alert.get("alert_type", "")
        impact_yuan_per_day = None

        if alert_type in ("price_spike", "price_drop"):
            price = alert.get("price") or alert.get("current_price")
            change_pct = alert.get("change_7d_pct", 0)
            if price and change_pct and avg_volume:
                impact_yuan_per_day = abs(price * change_pct / 100) / 7 * avg_volume

        elif alert_type == "regional_arbitrage":
            # spread = price column for arbitrage alerts; freight_cost from payload/message
            spread = alert.get("price", 0)
            freight = alert.get("freight_cost", 0)
            if avg_volume and spread:
                impact_yuan_per_day = (spread - freight) * avg_volume

        return {
            "impact_yuan_per_day": impact_yuan_per_day,
            "avg_daily_volume_tons": avg_volume,
        }

    # ── Rank ────────────────────────────────────────────────────────────

    def rank(self, items: list[dict]) -> list[dict]:
        """Sort by abs(impact_yuan_per_day) descending, take top N.

        Items with null impact go to the end (severity-based fallback).
        """
        top_n = settings.ALERT_DIGEST_TOP_N

        def sort_key(item):
            impact = item.get("impact_yuan_per_day")
            if impact is not None:
                return (0, -abs(impact))
            sev = {"high": 3, "medium": 2, "low": 1}.get(item.get("severity", ""), 0)
            return (1, -sev)

        items.sort(key=sort_key)
        return items[:top_n]

    # ── Template rendering ──────────────────────────────────────────────

    def render_template(self, items: list[dict]) -> str:
        """Markdown template rendering — used when LLM narrator fails or is disabled."""
        lines = [f"## 晨报 — {self._today_str()}", ""]
        for i, item in enumerate(items, 1):
            name = item.get("product_name", item.get("product_id", "Unknown"))
            atype = item.get("alert_type", "alert")
            impact = item.get("impact_yuan_per_day")
            impact_str = f"¥{impact:,.0f}/day" if impact else "impact TBD"
            change = item.get("change_7d_pct")
            change_str = f"{change:+.1f}%" if change else ""
            lines.append(f"{i}. **{name}** — {atype} {change_str} ({impact_str})")
        return "\n".join(lines)

    # ── Volume lookup (ERP) ─────────────────────────────────────────────

    def _get_avg_daily_volume(self, product_name: str) -> float | None:
        """Query erp_product_sales_details for 30d trailing avg shipment_quantity.

        Matches by FNAME column. Returns None on failure (graceful degradation).
        """
        if not product_name:
            return None
        try:
            result = self.db.execute(
                text(
                    """
                    SELECT AVG(shipment_quantity) AS avg_qty
                    FROM erp_product_sales_details
                    WHERE FNAME = :fname
                      AND shipment_date >= NOW() - INTERVAL '30 days'
                    """
                ),
                {"fname": product_name},
            ).fetchone()
            return float(result.avg_qty) if result and result.avg_qty else None
        except Exception:
            logger.warning(
                "Failed to query ERP volume for %s", product_name, exc_info=True
            )
            return None

    # ── Dedup ───────────────────────────────────────────────────────────

    def _today_digest_exists(self) -> bool:
        """Check if today's digest already exists for this org."""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        query = self.db.query(AlertLog).filter(
            AlertLog.alert_type == "daily_digest",
            AlertLog.triggered_at >= today_start,
        )
        if self.org_id:
            query = query.filter(AlertLog.org_id == self.org_id)
        return query.first() is not None

    # ── Helpers ─────────────────────────────────────────────────────────

    def _today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _persist(self, items: list[dict], narrative: str, narrator: str) -> AlertLog:
        """Persist digest as a single AlertLog row."""
        digest = AlertLog(
            org_id=self.org_id,
            product_id="digest",
            product_name="Morning Brief",
            alert_type="daily_digest",
            severity="high",
            message=narrative,
            payload={
                "items": items,
                "date": self._today_str(),
                "narrator": narrator,
            },
            source="digest_service",
            triggered_at=datetime.now(timezone.utc),
        )
        self.db.add(digest)
        self.db.commit()
        self.db.refresh(digest)
        return digest

    @staticmethod
    def _alert_to_dict(alert: AlertLog) -> dict:
        """Convert AlertLog ORM to dict for processing."""
        return {
            "id": str(alert.id),
            "product_id": alert.product_id,
            "product_name": alert.product_name,
            "alert_type": alert.alert_type,
            "severity": alert.severity,
            "price": alert.price,
            "change_7d_pct": alert.change_7d_pct,
            "message": alert.message,
            "triggered_at": alert.triggered_at.isoformat() if alert.triggered_at else None,
        }
