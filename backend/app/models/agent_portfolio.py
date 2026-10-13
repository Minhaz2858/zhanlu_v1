"""Agent portfolio — the AI-CoE use-case registry (IBM Gap 2, 2026-10-13).

IBM's AI Center-of- Excellence framework: a CoE maintains a PORTFOLIO of
AI use cases, each with a business owner, a KPI it moves, and a lifecycle
stage (pilot → production → …) — the structure that separates
organizations that scale AI from the 95% whose pilots die. Zhanlu's agent
catalog had versions/risk/red-team (ADLC) but no business dimension.

One entry per (agent, org): who owns the use case, what business metric
it is accountable for, where it sits in the lifecycle. The portfolio
router joins this with the ROI layer so leadership sees ownership +
value together.
"""

from typing import Optional

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

#: Lifecycle stages — mirrors the IBM maturity framing (pilot → production
#: → scale → embedded) collapsed to the four states that matter for a
#: portfolio board.
LIFECYCLE_STAGES = ("idea", "pilot", "production", "retired")


class AgentPortfolioEntry(TimestampedBase):
    """Business metadata for one agent use case, per tenant."""

    __tablename__ = "agent_portfolio_entries"
    __table_args__ = (
        UniqueConstraint("agent_id", "org_id", name="uq_portfolio_agent_org"),
    )

    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The business dimension the catalog never had:
    business_owner: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    business_kpi: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        doc="The business metric this use case is accountable for moving "
            "(e.g. 'quote turnaround time', 'month-end close effort').",
    )
    lifecycle_stage: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pilot"
    )
    value_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
