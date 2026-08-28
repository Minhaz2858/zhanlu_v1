"""CadBuildContract — design-intent contract for CAD agent builds.

Stored per conversation; verify_build validates live geometry against the
STORED contract (never the model's hand-picked verify args). Rows cascade
with the conversation (hard-delete rule).
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class CadBuildContract(TimestampedBase):
    __tablename__ = "cad_build_contracts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    app_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    contract_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
