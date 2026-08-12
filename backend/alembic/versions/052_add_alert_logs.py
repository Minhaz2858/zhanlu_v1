"""add alert_logs table for proactive market alerts

Revision ID: 052
Revises: 051
Create Date: 2026-08-12

Ported from EDIA 5.1.2 MySQL ``alerts_log`` → PostgreSQL ``alert_logs``.
Indexes support: unread count queries, dedup lookups, per-org listing.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "052_add_alert_logs"
down_revision = "051_automation_task_project_id_backfill"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alert_logs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=True),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("product_name", sa.String(), nullable=True),
        sa.Column("alert_type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), server_default="medium"),
        sa.Column("change_7d_pct", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(), server_default="perception"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_alert_logs_unread",
        "alert_logs",
        ["org_id", "dismissed_at", "read_at"],
    )

    op.create_index(
        "ix_alert_logs_dedup",
        "alert_logs",
        ["org_id", "product_id", "alert_type", "triggered_at"],
    )

    op.create_index(
        "ix_alert_logs_org_triggered",
        "alert_logs",
        ["org_id", "dismissed_at", "triggered_at"],
    )


def downgrade():
    op.drop_index("ix_alert_logs_org_triggered", table_name="alert_logs")
    op.drop_index("ix_alert_logs_dedup", table_name="alert_logs")
    op.drop_index("ix_alert_logs_unread", table_name="alert_logs")
    op.drop_table("alert_logs")
