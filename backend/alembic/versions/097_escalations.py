"""P3-6: escalations — structured human handoff with context transfer.

Revision ID: 097_escalations
Revises: 096_ontology_sensors
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "097_escalations"
down_revision = "096_ontology_sensors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "escalations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=False, index=True,
                  server_default="default-app"),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("context_json", sa.Text, nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True, index=True),
        sa.Column("agent_name", sa.String(100), nullable=True),
        sa.Column("object_name", sa.String(100), nullable=True),
        sa.Column("sentiment", sa.String(30), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("resolution_note", sa.Text, nullable=True),
        sa.Column("resolved_by", sa.String(36), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.execute("ALTER TABLE escalations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE escalations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON escalations USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON escalations")
    op.drop_table("escalations")
