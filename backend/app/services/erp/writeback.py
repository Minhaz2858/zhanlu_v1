"""Ecisco BI ERP Writeback — stage pricing decisions for ERP review.

Port of EDIA 5.1.2 backend/src/service/erp_writeback_service.py.
Adapted: EDIA SQLite staging → Zhanlu PostgreSQL + human-in-the-loop gate.

Workflow:
  1. Compute pricing decision via chat pipeline
  2. Stage recommendation with status=pending
  3. ERP admin reviews and approves/rejects
  4. Approved records are written to ERP live tables
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_VALID_PRODUCTS: set[str] = {
    "isoprene", "piperylene", "dcpd", "cracked_c5", "cracked_c9",
    "mixed_c5", "mixed_c9", "raffinate_c5", "sis",
    "c5_resin", "blowing_agent", "styrene",
}


class ErpWritebackService:
    """Stage pricing recommendations for ERP review."""

    def __init__(self, db=None, org_id: str | None = None):
        self.db = db
        self.org_id = org_id

    def stage_recommendation(
        self,
        product_id: str,
        recommended_price: float,
        valid_from: str | None = None,
        valid_until: str | None = None,
        rationale: str | None = None,
        submitted_by: str = "ecisco_bi_assistant",
        session_id: str | None = None,
    ) -> dict:
        """Stage a new pricing recommendation (pending approval)."""
        if product_id not in _VALID_PRODUCTS:
            return {"success": False, "error": f"Unknown product_id: {product_id}"}

        if valid_from is None:
            valid_from = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rec_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "id": rec_id,
            "product_id": product_id,
            "recommended_price": recommended_price,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "rationale": rationale or "",
            "submitted_by": submitted_by,
            "session_id": session_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }

        # In-memory staging (DB persistence added when ERP MySQL is wired)
        logger.info(
            "Staged recommendation %s: %s price=%s CNY validity=%s",
            rec_id, product_id, recommended_price, valid_from,
        )

        return {"success": True, "record": record, "stub": True}

    def get_pending_recommendations(self, product_id: str | None = None) -> list[dict]:
        """List pending recommendations awaiting review."""
        # Stub — real implementation queries DB
        return []

    def approve_recommendation(self, record_id: str, approved_by: str) -> dict:
        """Approve a pending recommendation."""
        return {
            "success": True,
            "record_id": record_id,
            "status": "approved",
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "stub": True,
        }
