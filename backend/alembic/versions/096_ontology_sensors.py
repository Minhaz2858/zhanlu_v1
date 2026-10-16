"""P3-2: ontology sensors — scheduled data watches that trigger fan-out.

Revision ID: 096_ontology_sensors
Revises: 095_procurement_po_drafts
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "096_ontology_sensors"
down_revision = "095_procurement_po_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ontology_sensors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=False, index=True,
                  server_default="default-app"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("object_name", sa.String(100), nullable=False),
        sa.Column("kb_id", sa.String(36), nullable=False, index=True),
        sa.Column("watch_sql", sa.Text, nullable=False),
        sa.Column("op", sa.String(10), nullable=False, server_default="lt"),
        sa.Column("threshold", sa.Float, nullable=False, server_default="0"),
        sa.Column("schedule_seconds", sa.Integer, nullable=False,
                  server_default="3600"),
        sa.Column("team_id", sa.String(36), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_run_at", sa.String(40), nullable=True),
        sa.Column("last_state_json", sa.Text, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "ontology_sensor_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=False, index=True,
                  server_default="default-app"),
        sa.Column("sensor_id", sa.String(36), nullable=False, index=True),
        sa.Column("value", sa.Float, nullable=True),
        sa.Column("breached", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("transition", sa.String(20), nullable=False, server_default="none"),
        sa.Column("change_summary", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    for table in ("ontology_sensors", "ontology_sensor_runs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ("
            "  coalesce(current_setting('app.current_org', true), '') = ''"
            "  OR org_id = current_setting('app.current_org', true))"
        )


def downgrade() -> None:
    for table in ("ontology_sensor_runs", "ontology_sensors"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
