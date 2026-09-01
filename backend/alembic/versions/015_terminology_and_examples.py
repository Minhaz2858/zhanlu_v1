"""015_terminology_and_examples

Revision ID: 015
Revises: 014
Create Date: 2026-07-15

Add ``terminologies`` and ``q_sql_examples`` tables for NL2SQL metadata enrichment.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── terminologies ──────────────────────────────────────────────
    op.create_table(
        "terminologies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("word", sa.String(255), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("terminologies.id"), nullable=True, index=True),
        sa.Column("datasource_ids", sa.Text, nullable=True),
        sa.Column("agent_id", sa.String(36), nullable=True, index=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("embedding_text", sa.Text, nullable=True),
        # TimestampedBase columns
        sa.Column("created_date", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_date", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org", index=True),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app", index=True),
    )

    # ── q_sql_examples ─────────────────────────────────────────────
    op.create_table(
        "q_sql_examples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("sql", sa.Text, nullable=False),
        sa.Column("datasource_id", sa.String(36), nullable=True, index=True),
        sa.Column("agent_id", sa.String(36), nullable=True, index=True),
        sa.Column("embedding_text", sa.Text, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        # TimestampedBase columns
        sa.Column("created_date", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_date", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org", index=True),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app", index=True),
    )


def downgrade() -> None:
    op.drop_table("q_sql_examples")
    op.drop_table("terminologies")
