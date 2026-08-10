"""046 — add project_id column to agent_memories.

Revision ID: 046
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa


revision = "046_agent_memories_project_id"
down_revision = "045_llm_models"
branch_labels = None
depends_on = None


def upgrade():
    # The SQLAlchemy model has had this column since the 2026-08-05
    # project-scoping change (see MEMORY note re: Q2 2026 sales report
    # leak), but the actual CREATE TABLE was never migrated. Backfill
    # the column so memory_consolidation's INSERT doesn't crash with
    # "column agent_memories.project_id does not exist".
    #
    # Note: the same column is added idempotently on every startup by
    # main._run_auto_migrations() using ``ALTER TABLE ... ADD COLUMN
    # IF NOT EXISTS``. This Alembic migration is the canonical record for
    # fresh environments — its ADD COLUMN is guarded by a column
    # existence check so a row that's already been added by the runtime
    # path doesn't break ``alembic upgrade head``.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = {c["name"] for c in inspector.get_columns("agent_memories")}
    if "project_id" not in cols:
        op.add_column(
            "agent_memories",
            sa.Column("project_id", sa.String(36), nullable=True),
        )
    # Indexes: similarly idempotent
    existing_idx = {ix["name"] for ix in inspector.get_indexes("agent_memories")}
    if "ix_agent_memories_project_id" not in existing_idx:
        op.create_index(
            "ix_agent_memories_project_id",
            "agent_memories",
            ["project_id"],
            unique=False,
        )


def downgrade():
    op.drop_index("ix_agent_memories_project_id", table_name="agent_memories")
    op.drop_column("agent_memories", "project_id")
