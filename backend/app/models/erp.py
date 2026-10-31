"""ERP submission ledger (EBOM Workstream C) — connector-neutral write-back.

One row per (org, idempotency_key). A submission is the ONLY record of an
external ERP write: created as `executing` before the adapter call, updated
to `succeeded`/`failed` after. The unique constraint makes duplicate
execution impossible at the storage layer, whatever the caller path.
"""

from __future__ import annotations

from sqlalchemy import JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampedBase


class ErpSubmission(TimestampedBase, Base):
    __tablename__ = "erp_submissions"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key",
                         name="uq_erp_submission_idempotency"),
    )

    draft_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    connector_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                         default="executing")
    external_ref: Mapped[str | None] = mapped_column(String(64))
    request_json: Mapped[dict | None] = mapped_column(JSON)
    response_json: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[str | None] = mapped_column(String(36), index=True)
