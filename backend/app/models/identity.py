"""Cross-source identity resolution models (PRD P3 + P3a).

FileTableSchema / FileTableRow — structured staging of spreadsheet KBs
(P3a prerequisite: uploaded xlsx/csv previously existed only as text
chunks; identity resolution needs queryable records).

IdentityMatch — crosswalk rows: "real-world entity X in source A ≡ row
pk_b in source B". NEVER merges or copies source records; the source
systems stay authoritative. Tiers 1-2 (deterministic, evidence-backed)
auto-confirm; tiers 3-4 land in the human review queue. Rejections are
remembered and never re-proposed by the same method.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

MATCH_TIERS = ("exact", "normalization", "semantic", "probabilistic")
MATCH_STATUSES = ("confirmed", "inferred", "rejected")


class FileTableSchema(TimestampedBase):
    __tablename__ = "file_table_schemas"

    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    columns_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class FileTableRow(TimestampedBase):
    __tablename__ = "file_table_rows"

    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    row_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    values_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class IdentityMatch(TimestampedBase):
    __tablename__ = "identity_matches"
    __table_args__ = (
        UniqueConstraint("org_id", "object_id", "source_a_key", "pk_a",
                         "source_b_key", "pk_b",
                         name="uq_identity_match_pair"),
    )

    object_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source_a: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    source_b: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    source_a_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_b_key: Mapped[str] = mapped_column(String(255), nullable=False)
    pk_a: Mapped[str] = mapped_column(String(255), nullable=False)
    pk_b: Mapped[str] = mapped_column(String(255), nullable=False)
    match_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    normalization_rule_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="inferred"
    )
    evidence_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    summary_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
