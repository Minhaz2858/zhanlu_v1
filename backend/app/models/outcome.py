"""Outcome measurements (EBOM Workstream B) — governed realized impact.

One row per (org, decision, metric, baseline-period, observed-period).
Baseline and observed values are computed ONLY through the governed
MetricDefinition path (compute_metric) — never claimed, never LLM-computed.
Expected impact is what a human declared; realized impact is what the
governed metrics actually show; verified impact is what an authorized
reviewer certified. These three are NEVER conflated.
"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import JSON, DateTime, Float, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampedBase


class OutcomeMeasurement(TimestampedBase, Base):
    __tablename__ = "outcome_measurements"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "approval_id", "metric_definition_id",
            "baseline_period", "observed_period",
            name="uq_outcome_decision_metric_periods",
        ),
    )

    # Decision lineage (all nullable — a measurement may precede formal refs)
    approval_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action_id: Mapped[str | None] = mapped_column(String(36))
    escalation_id: Mapped[str | None] = mapped_column(String(36))
    draft_id: Mapped[str | None] = mapped_column(String(36), index=True)

    # Governed metric anchor
    metric_definition_id: Mapped[str] = mapped_column(String(36), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Business expectation declared by a human (margin: increase; cost: decrease)
    direction: Mapped[str] = mapped_column(String(10), nullable=False,
                                           default="increase")
    expected_impact: Mapped[float | None] = mapped_column(Float)

    # Governed computation results
    baseline_value: Mapped[float | None] = mapped_column(Float)
    observed_value: Mapped[float | None] = mapped_column(Float)
    baseline_period: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_period: Mapped[str] = mapped_column(String(20), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(30))
    realized_impact: Mapped[float | None] = mapped_column(Float)

    measurement_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending")  # pending|measured|failed
    measurement_note: Mapped[str | None] = mapped_column(Text)

    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unverified")  # unverified|verified|rejected
    verified_by: Mapped[str | None] = mapped_column(String(36))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    verification_note: Mapped[str | None] = mapped_column(Text)

    evidence_refs: Mapped[dict | None] = mapped_column(JSON)
