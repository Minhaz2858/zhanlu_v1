"""047 — add catalog_status column to knowledge_bases.

Revision ID: 047
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa


revision = "047_knowledge_bases_catalog_status"
down_revision = "046_agent_memories_project_id"
branch_labels = None
depends_on = None


def upgrade():
    """Add ``catalog_status`` (String(16), default 'pending', indexed) to
    ``knowledge_bases`` — declared on the SQLAlchemy model on 2026-08-10
    for the Project Knowledge Graph spec, but never migrated.

    Symptom of the missing column: every read on the KB table (generic
    ``GET /entities/KnowledgeBase``, Connectors tab in MySpace, the
    "Add Data Sources" modal that picks existing KBs into a project, the
    auto-migrate ``prepare_data_source_runtime`` probe) raised
    ``psycopg2.errors.UndefinedColumn: column knowledge_bases.catalog_status
    does not exist``. The frontend's try/except swallowed it and showed
    "No knowledge bases available." on the Add dialog — making it look
    like no KBs existed when in fact the API was returning 500.

    Idempotent: column existence is checked first so re-running on an
    already-migrated DB is a no-op (the auto-migrate path may also
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` on startup, which is
    fine — Postgres treats them as equivalent).
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = {c["name"] for c in inspector.get_columns("knowledge_bases")}
    if "catalog_status" not in cols:
        op.add_column(
            "knowledge_bases",
            sa.Column(
                "catalog_status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
            ),
        )
    existing_idx = {ix["name"] for ix in inspector.get_indexes("knowledge_bases")}
    if "ix_knowledge_bases_catalog_status" not in existing_idx:
        op.create_index(
            "ix_knowledge_bases_catalog_status",
            "knowledge_bases",
            ["catalog_status"],
            unique=False,
        )
    # Backfill: any pre-existing rows have the server_default value, which
    # is exactly what ``mapped_column(default="pending")`` would have given
    # them at INSERT time. No UPDATE needed.


def downgrade():
    op.drop_index("ix_knowledge_bases_catalog_status", table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "catalog_status")