"""P4 T7 — ontology_document_decision_events: append-only decision history
for document-derived ontology proposals (IdentityDecisionEvent pattern).

Revision ID: 109_document_decision_events
Revises: 108_ontology_document_proposals
Create Date: 2026-10-31

One row per review event (revise / approve / reject / supersede). Rows are
NEVER updated or deleted — history is reconstructed by ordering on
(event_seq, created_date), anchored to the immutable doc_version_id.
"""

from alembic import op
import sqlalchemy as sa

revision = "109_document_decision_events"
down_revision = "108_ontology_document_proposals"
branch_labels = None
depends_on = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def _common_cols() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "ontology_document_decision_events",
        *_common_cols(),
        sa.Column("proposal_id", sa.String(36), nullable=False, index=True),
        sa.Column("object_id", sa.String(36), nullable=True, index=True),
        sa.Column("event_kind", sa.String(20), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("previous_status", sa.String(20), nullable=True),
        sa.Column("new_status", sa.String(20), nullable=False),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("org_id", "proposal_id", "event_seq",
                            name="uq_document_decision_event_seq"),
    )
    _rls("ontology_document_decision_events")


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation "
        "ON ontology_document_decision_events"
    )
    op.drop_table("ontology_document_decision_events")
