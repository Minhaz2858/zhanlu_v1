"""Cost service — tracks and enforces costs for every operation.

Records LLM token usage, sandbox job costs, artifact build costs, and
storage costs in the cost_ledger table.  Provides aggregation queries
for budget enforcement and reporting.
"""

import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.governance import CostLedger, COST_TYPES

logger = logging.getLogger(__name__)

# Approximate cost per 1K tokens (USD) — configurable
TOKEN_COSTS = {
    "gpt-4o": {"prompt": 0.0025, "completion": 0.01},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4": {"prompt": 0.03, "completion": 0.06},
    "gpt-3.5-turbo": {"prompt": 0.0005, "completion": 0.0015},
}

# Sandbox cost per second (USD)
SANDBOX_COST_PER_SECOND = 0.001


class CostService:
    """Service for tracking and enforcing operation costs."""

    def __init__(self, db: Session):
        self.db = db

    def record_llm_cost(
        self,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        execution_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> CostLedger:
        """Record the cost of an LLM call."""
        total_tokens = prompt_tokens + completion_tokens
        cost = self._calculate_llm_cost(model_name, prompt_tokens, completion_tokens)

        entry = CostLedger(
            id=str(uuid4()),
            cost_type="llm_tokens",
            cost_amount=cost,
            cost_currency="USD",
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            execution_id=execution_id,
            conversation_id=conversation_id,
            agent_name=agent_name,
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def record_sandbox_cost(
        self,
        sandbox_job_id: str,
        duration_seconds: float,
        execution_id: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> CostLedger:
        """Record the cost of a sandbox job."""
        cost = duration_seconds * SANDBOX_COST_PER_SECOND

        entry = CostLedger(
            id=str(uuid4()),
            cost_type="sandbox_job",
            cost_amount=cost,
            cost_currency="USD",
            sandbox_job_id=sandbox_job_id,
            sandbox_duration_seconds=duration_seconds,
            execution_id=execution_id,
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def record_artifact_build_cost(
        self,
        artifact_id: str,
        cost_amount: float = 0.01,
        execution_id: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> CostLedger:
        """Record the cost of an artifact build."""
        entry = CostLedger(
            id=str(uuid4()),
            cost_type="artifact_build",
            cost_amount=cost_amount,
            cost_currency="USD",
            execution_id=execution_id,
            metadata_json={"artifact_id": artifact_id},
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def get_total_cost(
        self,
        execution_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> dict:
        """Get total cost with optional filters."""
        query = self.db.query(
            func.sum(CostLedger.cost_amount).label("total_cost"),
            func.count(CostLedger.id).label("entry_count"),
        )

        if execution_id:
            query = query.filter(CostLedger.execution_id == execution_id)
        if conversation_id:
            query = query.filter(CostLedger.conversation_id == conversation_id)
        if org_id:
            query = query.filter(CostLedger.org_id == org_id)

        result = query.first()
        return {
            "total_cost": float(result.total_cost or 0),
            "entry_count": result.entry_count or 0,
        }

    def get_cost_breakdown(
        self,
        execution_id: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> list[dict]:
        """Get cost breakdown by type."""
        query = self.db.query(
            CostLedger.cost_type,
            func.sum(CostLedger.cost_amount).label("total"),
            func.count(CostLedger.id).label("count"),
        )

        if execution_id:
            query = query.filter(CostLedger.execution_id == execution_id)
        if org_id:
            query = query.filter(CostLedger.org_id == org_id)

        query = query.group_by(CostLedger.cost_type)
        results = query.all()

        return [
            {"cost_type": r.cost_type, "total_cost": float(r.total or 0), "count": r.count}
            for r in results
        ]

    def _calculate_llm_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate the cost of an LLM call based on token usage."""
        model_key = model.lower()
        for key, rates in TOKEN_COSTS.items():
            if key in model_key:
                return (prompt_tokens / 1000 * rates["prompt"]) + (completion_tokens / 1000 * rates["completion"])

        # Default rate if model not found
        return (prompt_tokens / 1000 * 0.001) + (completion_tokens / 1000 * 0.002)
