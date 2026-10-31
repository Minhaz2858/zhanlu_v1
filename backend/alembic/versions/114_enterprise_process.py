"""EBPM — enterprise business process discovery models.

Revision ID: 114_enterprise_process
Revises: 113_outcome_measurements_erp
Create Date: 2026-10-31

ADDITIVE ONLY. Seven org-scoped tables (RLS enforced) for the dynamically
discovered Enterprise Business Process & Operating Model:
- enterprise_resources: refreshable inventory of authorized resources
- org_units / business_capabilities: discovered organization + capability map
- business_processes / process_activities / process_links: discovered
  end-to-end processes with ordered activities and dependencies
- process_evidence: verbatim evidence quotes backing every discovered element
"""

from alembic import op
import sqlalchemy as sa

revision = "114_enterprise_process"
down_revision = "113_outcome_measurements_erp"
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


def _base_cols() -> list:
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


TABLES = [
    ("enterprise_resources", [
        sa.Column("resource_kind", sa.String(40), nullable=False, index=True),
        sa.Column("ref_id", sa.String(120), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status_json", sa.JSON(), nullable=True),
        sa.Column("evidence_status", sa.String(20), nullable=False,
                  server_default="verified"),
        sa.Column("discovered_at", sa.String(40), nullable=True),
        sa.UniqueConstraint("org_id", "resource_kind", "ref_id",
                            name="uq_ent_res"),
    ]),
    ("org_units", [
        sa.Column("unit_type", sa.String(20), nullable=False,
                  server_default="department"),
        sa.Column("name", sa.String(255), nullable=False, index=True),
        sa.Column("parent_name", sa.String(255), nullable=True),
        sa.Column("responsibilities", sa.Text(), nullable=True),
        sa.Column("evidence_status", sa.String(20), nullable=False,
                  server_default="inferred"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("org_id", "name", "unit_type", name="uq_org_unit"),
    ]),
    ("business_capabilities", [
        sa.Column("name", sa.String(255), nullable=False, index=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("supports_json", sa.JSON(), nullable=True),
        sa.Column("evidence_status", sa.String(20), nullable=False,
                  server_default="inferred"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("org_id", "name", name="uq_capability"),
    ]),
    ("business_processes", [
        sa.Column("name", sa.String(255), nullable=False, index=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("trigger_event", sa.Text(), nullable=True),
        sa.Column("objective", sa.Text(), nullable=True),
        sa.Column("start_condition", sa.Text(), nullable=True),
        sa.Column("completion_condition", sa.Text(), nullable=True),
        sa.Column("process_family", sa.String(120), nullable=True, index=True),
        sa.Column("discovery_status", sa.String(20), nullable=False,
                  server_default="documented"),
        sa.Column("evidence_status", sa.String(20), nullable=False,
                  server_default="proposed"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance_json", sa.JSON(), nullable=True),
        sa.Column("kpis_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("org_id", "name", name="uq_process"),
    ]),
    ("process_activities", [
        sa.Column("process_id", sa.String(36), nullable=False, index=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("activity_type", sa.String(30), nullable=False,
                  server_default="human_task"),
        sa.Column("performer", sa.String(255), nullable=True),
        sa.Column("input_refs", sa.JSON(), nullable=True),
        sa.Column("output_refs", sa.JSON(), nullable=True),
        sa.Column("business_rules", sa.Text(), nullable=True),
        sa.Column("exception_paths", sa.Text(), nullable=True),
        sa.Column("evidence_status", sa.String(20), nullable=False,
                  server_default="inferred"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("org_id", "process_id", "seq", "name",
                            name="uq_activity"),
    ]),
    ("process_links", [
        sa.Column("predecessor_id", sa.String(36), nullable=False, index=True),
        sa.Column("successor_id", sa.String(36), nullable=False, index=True),
        sa.Column("handoff_note", sa.Text(), nullable=True),
        sa.Column("evidence_status", sa.String(20), nullable=False,
                  server_default="inferred"),
        sa.UniqueConstraint("org_id", "predecessor_id", "successor_id",
                            name="uq_plink"),
    ]),
    ("process_evidence", [
        sa.Column("process_id", sa.String(36), nullable=False, index=True),
        sa.Column("activity_seq", sa.Integer(), nullable=True),
        sa.Column("evidence_kind", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=False),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.UniqueConstraint("org_id", "process_id", "source_ref",
                            name="uq_pevidence"),
    ]),
]


def upgrade() -> None:
    for table, cols in TABLES:
        op.create_table(table, *_base_cols(), *cols)
        _rls(table)


def downgrade() -> None:
    for table, _cols in reversed(TABLES):
        op.drop_table(table)
