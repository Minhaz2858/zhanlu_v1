"""cad_revisions — engineering-change revision store (roadmap P3).

Revision ID: 101_cad_revisions
Revises: 100_lineage_org_alignment
Create Date: 2026-10-19
"""

from alembic import op
import sqlalchemy as sa

revision = "101_cad_revisions"
down_revision = "100_lineage_org_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cad_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("app_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36),
                  sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("agent_id", sa.String(36), nullable=True),
        sa.Column("revision_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("parent_revision_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("change_summary", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(30), nullable=False,
                  server_default="pending_approval"),
        sa.Column("before_snapshot", sa.JSON(), nullable=True),
        sa.Column("after_snapshot", sa.JSON(), nullable=True),
        sa.Column("impact_report", sa.JSON(), nullable=True),
        sa.Column("approved_by_id", sa.String(36), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cad_revisions_conversation_id", "cad_revisions",
                    ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_cad_revisions_conversation_id", table_name="cad_revisions")
    op.drop_table("cad_revisions")
