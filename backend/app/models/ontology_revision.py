"""OntologyRevision — append-only version history for ontology elements
(PRD P2, gap G4).

One row per meaningful mutation of an ontology object/link/action/domain.
Transactionally atomic with the mutation it describes (same session).
History is NEVER updated or deleted; rollback appends a new revision.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

ENTITY_TYPES = ("object", "link", "action", "domain")
CHANGE_KINDS = ("create", "update", "bind", "unbind", "retire", "rollback",
                "drift")


class OntologyRevision(TimestampedBase):
    __tablename__ = "ontology_revisions"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "revision_no",
                         name="uq_ontology_revision_no"),
    )

    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    change_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    before_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    changed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    change_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
