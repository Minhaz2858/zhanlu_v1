"""Add payload JSONB column to alert_logs for digest data.

Revision ID: 053
Revises: 052
Create Date: 2026-08-12

The payload column stores structured data for daily_digest alerts:
{items: [...], date: "2026-08-12", narrator: "llm"|"template"}
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "053_add_alert_payload"
down_revision = "052_add_alert_logs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "alert_logs",
        sa.Column("payload", sa.JSON(), nullable=True, comment="Structured data for digest/alerts"),
    )


def downgrade():
    op.drop_column("alert_logs", "payload")
