"""AlertLog model — proactive market alert persistence.

Ported from EDIA 5.1.2 backend/src/service/alert_service.py
MySQL ``alerts_log`` → PostgreSQL ``alert_logs`` via SQLAlchemy ORM.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON

from app.database import Base


class AlertLog(Base):
    """Proactive market alert — price spike/drop, regional arbitrage, etc.

    Lifecycle: triggered_at → read_at → dismissed_at.
    Badge count = COUNT WHERE read_at IS NULL AND dismissed_at IS NULL.
    Panel shows all WHERE dismissed_at IS NULL.
    """

    __tablename__ = "alert_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # ── Tenant scoping ────────────────────────────────────────────────
    org_id = Column(String, nullable=True, index=True)

    # ── Alert target ───────────────────────────────────────────────────
    product_id = Column(String, nullable=False, index=True)
    product_name = Column(String, nullable=True)

    # ── Alert classification ──────────────────────────────────────────
    # price_spike | price_drop | regional_arbitrage | notification
    alert_type = Column(String, nullable=False, index=True)

    # high | medium | low
    severity = Column(String, default="medium")

    # ── Metrics ────────────────────────────────────────────────────────
    change_7d_pct = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    message = Column(Text, nullable=True)

    # ── Lifecycle timestamps ───────────────────────────────────────────
    triggered_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    read_at = Column(DateTime(timezone=True), nullable=True)
    dismissed_at = Column(DateTime(timezone=True), nullable=True)

    # ── Structured data (digest items, etc.) ──────────────────────────
    payload = Column(JSON, nullable=True, comment="Structured data for digest/alerts")

    # ── Provenance ─────────────────────────────────────────────────────
    # perception | intelligence | manual | digest_service
    source = Column(String, default="perception")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "org_id": self.org_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "change_7d_pct": self.change_7d_pct,
            "price": self.price,
            "message": self.message,
            "payload": self.payload,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "dismissed_at": self.dismissed_at.isoformat() if self.dismissed_at else None,
            "source": self.source,
        }
