"""P1 unified evidence taxonomy — evidence_status/confidence/provenance_json
on ontology_objects, ontology_links, ontology_actions.

Backfill rules (PRD 2026-10-30 — never auto-verify uncertain legacy rows):
- bound objects (backing_kind <> 'manual' AND non-empty backing_ref) →
  'inferred' + backfill provenance marker
- everything else → 'proposed' (server_default)
- actions with a bound tool → 'inferred'; others → 'proposed'
- links → 'proposed'
- confidence stays NULL everywhere (never fabricated)
- 'verified' is ONLY set by explicit human binds after this migration

Revision ID: 103_ontology_evidence
Revises: 102_turn_identity
Create Date: 2026-10-30
"""

from alembic import op
import sqlalchemy as sa

revision = "103_ontology_evidence"
down_revision = "102_turn_identity"
branch_labels = None
depends_on = None

_TABLES = ("ontology_objects", "ontology_links", "ontology_actions")

_BACKFILL_MARK = '{"backfill": "2026-10-30", "note": "binding predates evidence taxonomy"}'


def upgrade() -> None:
    for t in _TABLES:
        op.add_column(
            t,
            sa.Column("evidence_status", sa.String(20), nullable=False,
                      server_default="proposed"),
        )
        op.add_column(t, sa.Column("confidence", sa.Float(), nullable=True))
        op.add_column(t, sa.Column("provenance_json", sa.JSON(), nullable=True))
        op.create_index(f"ix_{t}_evidence_status", t, ["evidence_status"])

    # Backfill — bound rows carry real evidence but no recorded human
    # authorization: inferred, never verified.
    op.execute(
        "UPDATE ontology_objects SET evidence_status = 'inferred', "
        f"provenance_json = '{_BACKFILL_MARK}'::json "
        "WHERE backing_kind <> 'manual' "
        "AND backing_ref IS NOT NULL AND backing_ref <> ''"
    )
    op.execute(
        "UPDATE ontology_actions SET evidence_status = 'inferred' "
        "WHERE tool_name IS NOT NULL AND tool_name <> ''"
    )


def downgrade() -> None:
    for t in _TABLES:
        op.drop_index(f"ix_{t}_evidence_status", table=t)
        op.drop_column(t, "provenance_json")
        op.drop_column(t, "confidence")
        op.drop_column(t, "evidence_status")
