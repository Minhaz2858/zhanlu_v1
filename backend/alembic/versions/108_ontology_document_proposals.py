"""P4 T4 — ontology_document_proposals: document-derived ontology
proposals awaiting human review.

Revision ID: 108_ontology_document_proposals
Revises: 107_document_versions
Create Date: 2026-10-31

Idempotency: (org_id, kb_id, doc_version_id, chunk_key, proposal_kind,
quote_hash) — the same quote from the same chunk never proposes twice.
Authorization inheritance: source_kb_id + audience_json ride on the row
(D3/D6/D7).
"""

from alembic import op
import sqlalchemy as sa

revision = "108_ontology_document_proposals"
down_revision = "107_document_versions"
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
        "ontology_document_proposals",
        *_common_cols(),
        sa.Column("kb_id", sa.String(36), nullable=False, index=True),
        sa.Column("doc_version_id", sa.String(36), nullable=False, index=True),
        sa.Column("source_kb_id", sa.String(36), nullable=False),
        sa.Column("chunk_key", sa.String(255), nullable=False),
        sa.Column("proposal_kind", sa.String(40), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_hash", sa.String(64), nullable=False),
        sa.Column("location_json", sa.JSON(), nullable=True),
        sa.Column("method", sa.String(60), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=True),
        sa.Column("audience_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="proposed", index=True),
        sa.Column("object_id", sa.String(36), nullable=True, index=True),
        sa.Column("conflict_json", sa.JSON(), nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "org_id", "kb_id", "doc_version_id", "chunk_key",
            "proposal_kind", "quote_hash",
            name="uq_document_proposal_idempotency",
        ),
    )
    _rls("ontology_document_proposals")


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation "
        "ON ontology_document_proposals"
    )
    op.drop_table("ontology_document_proposals")
