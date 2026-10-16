"""Procurement write-back — the FIRST real business write (P0-4).

A PO draft is a durable procurement suggestion an agent creates AFTER a
human approved the ontology action (governance gate). It is a real
write-back record the buyer works from (edit → export → ERP entry) —
honest about not writing directly into the customer ERP until an ERP
write connector exists.
"""

from typing import Optional

from sqlalchemy import JSON, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

DRAFT_STATUSES = ("pending_approval", "approved", "rejected", "exported")


class ProcurementPoDraft(TimestampedBase):
    """An approval-gated purchase-order draft created by the platform."""

    __tablename__ = "procurement_po_drafts"

    supplier_name: Mapped[str] = mapped_column(String(255), nullable=False)
    supplier_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    product: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    est_unit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    est_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    justification: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # pending_approval is only possible BEFORE the governance gate — the
    # tool runs exclusively post-approval, so rows are born approved.
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="approved")
    approval_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    extras_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # P3-12: 'po' | 'quote' — quotes reuse the same approval-gated pipeline
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="po")
