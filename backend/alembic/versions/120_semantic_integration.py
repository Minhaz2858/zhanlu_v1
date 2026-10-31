"""120_semantic_integration — BusinessRule bridge targeting + terminology
ontology link.

Revision ID: 120_semantic_integration
Revises: 119_business_rule
Create Date: 2026-11-01

WHY
---
Phase 1B.2 wires the authoritative BusinessRule model into the existing
SQL semantic infrastructure (no second rule engine):

1. ``business_rules.datasource_ids`` (JSON, nullable) — optional KB
   targeting for ``applies_to='sql'`` rules. NULL = org-wide: the
   SemanticGuidelines bridge (compatibility layer) materializes derived
   rows for every KB of the org at sync time.
2. ``terminologies.ontology_object_id`` (String(36), nullable, indexed)
   — optional link from a glossary entry to a unified ontology object.
   Plain id, NO hard FK: org consistency is validated by the link API
   when linking (a hard FK would couple unrelated cleanup paths and
   reject ids whose target was hard-deleted in another org's timeline).

CHANGE (additive only)
----------------------
Two nullable columns + one index. No backfill; every existing row keeps
its meaning and unlinked/untargeted rows behave exactly as before.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "120_semantic_integration"
down_revision: Union[str, Sequence[str], None] = "119_business_rule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "business_rules",
        sa.Column("datasource_ids", sa.JSON(), nullable=True),
    )
    op.add_column(
        "terminologies",
        sa.Column("ontology_object_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_terminologies_ontology_object_id", "terminologies",
        ["ontology_object_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_terminologies_ontology_object_id",
                  table_name="terminologies")
    op.drop_column("terminologies", "ontology_object_id")
    op.drop_column("business_rules", "datasource_ids")
