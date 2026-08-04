"""Decision Audit Trail — track and compare decisions over time.

Port of EDIA 5.1.2 backend/src/service/decision_audit.py.
Adapted: SQLite → in-memory cache + optional async SQLAlchemy,
module-level singleton → class-based constructor.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class DecisionAuditRecord:
    def __init__(
        self, product_id: str, date: str, recommendation_price: float,
        confidence: float, reasoning_hash: str, factors: dict, scenario: dict | None = None,
    ):
        self.product_id = product_id
        self.date = date
        self.recommendation_price = recommendation_price
        self.confidence = confidence
        self.reasoning_hash = reasoning_hash
        self.factors = factors
        self.scenario = scenario or {}

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id, "date": self.date,
            "recommendation_price": self.recommendation_price,
            "confidence": self.confidence, "reasoning_hash": self.reasoning_hash,
            "factors": self.factors, "scenario": self.scenario,
        }


class DecisionAuditService:
    """Track decision history with in-memory cache + optional DB persistence."""

    MAX_HISTORY_PER_PRODUCT = 50

    def __init__(self, db=None, org_id: str | None = None):
        self.db = db
        self.org_id = org_id
        self._cache: dict[str, list[DecisionAuditRecord]] = {}

    def record_decision(
        self, product_id: str, recommendation_price: float, confidence: float,
        factors: dict, scenario: dict | None = None,
    ) -> DecisionAuditRecord:
        date = datetime.now(timezone.utc).isoformat()
        reasoning_hash = self._hash_factors(factors)
        record = DecisionAuditRecord(
            product_id=product_id, date=date,
            recommendation_price=recommendation_price, confidence=confidence,
            reasoning_hash=reasoning_hash, factors=factors, scenario=scenario,
        )
        records = self._cache.setdefault(product_id, [])
        records.append(record)
        if len(records) > self.MAX_HISTORY_PER_PRODUCT:
            records.pop(0)
        logger.info("Audit recorded: %s price=%s conf=%.0f%% hash=%s",
                     product_id, recommendation_price, int(confidence * 100), reasoning_hash[:8])
        return record

    def get_history(self, product_id: str, limit: int = 10) -> list[dict]:
        records = self._cache.get(product_id, [])
        return [r.to_dict() for r in records[-limit:]]

    def compare_with_history(self, product_id: str, current_price: float) -> dict:
        records = self._cache.get(product_id, [])
        if len(records) < 2:
            return {"can_compare": False}
        prev = records[-2]
        recent = records[-1]
        return {
            "can_compare": True,
            "previous": prev.to_dict(),
            "current": recent.to_dict(),
            "price_delta": round(current_price - prev.recommendation_price, 2),
            "confidence_delta": round(recent.confidence - prev.confidence, 3),
            "reasoning_changed": recent.reasoning_hash != prev.reasoning_hash,
        }

    @staticmethod
    def _hash_factors(factors: dict) -> str:
        raw = json.dumps(factors, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def audit_summary(records: list[dict]) -> str:
        if not records:
            return "No audit history."
        lines = [f"Audit trail ({len(records)} records):"]
        for r in records[-5:]:
            lines.append(
                f"  {r['date'][:16]}: {r['recommendation_price']:,}元 "
                f"(conf={int(r['confidence']*100)}%, hash={r['reasoning_hash'][:6]})"
            )
        return "\n".join(lines)
