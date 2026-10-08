"""RedTeamResult — adversarial campaign results per agent (ADLC P1-4)."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class RedTeamResult(TimestampedBase):
    __tablename__ = "red_team_results"

    agent_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    agent_app_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    probe_name: Mapped[str] = mapped_column(String(100), nullable=False)
    probe_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True, default="medium")
    response_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_probes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "campaign_id": self.campaign_id,
            "probe_name": self.probe_name,
            "passed": bool(self.passed),
            "severity": self.severity,
            "response_excerpt": (self.response_excerpt or "")[:500],
            "total_probes": self.total_probes,
            "details": self.details or {},
            "created_date": self.created_date.isoformat() if self.created_date else None,
        }
