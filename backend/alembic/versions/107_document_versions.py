"""P4 T1 — document versioning: immutable document_versions + current
pointer columns on knowledge_bases.

Revision ID: 107_document_versions
Revises: 106_schema_classification
Create Date: 2026-10-31

D5: version rows are IMMUTABLE — never updated, never deleted. The
current-version pointer lives on knowledge_bases (content_hash +
content_version_id).
"""

from alembic import op
import sqlalchemy as sa

revision = "107_document_versions"
down_revision = "106_schema_classification"
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
        "document_versions",
        *_common_cols(),
        sa.Column("kb_id", sa.String(36),
                  sa.ForeignKey("knowledge_bases.id"),
                  nullable=False, index=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_bytes", sa.Integer(), nullable=True),
        sa.Column("file_url_at_ingest", sa.String(500), nullable=True),
        sa.Column("first_indexed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "kb_id", "content_hash",
                            name="uq_document_version_hash"),
    )
    _rls("document_versions")

    # Current-version pointer (the only mutable state — D5).
    op.add_column(
        "knowledge_bases",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("content_version_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_bases", "content_version_id")
    op.drop_column("knowledge_bases", "content_hash")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON document_versions")
    op.drop_table("document_versions")
