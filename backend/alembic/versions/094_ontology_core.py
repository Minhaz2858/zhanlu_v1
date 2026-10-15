"""Ontology core — P0-2 of the Enterprise AI OS strategy (2026-10-15).

Six tables composing the fourfold layer (data + logic + action + security):
ontology_domains / ontology_objects / ontology_links / ontology_actions /
agent_domain_bindings / coverage_states. All org-scoped → RLS applied here
inline: the tables are created EMPTY (no ACCESS EXCLUSIVE contention), so
FORCE RLS at creation is safe — unlike later ALTERs on live tables which
need the stop-app window (scripts/apply_rls*.sql).

Revision ID: 094_ontology_core
Revises: 093_rename_relevance_to_target
"""

import sqlalchemy as sa
from alembic import op

revision = "094_ontology_core"
down_revision = "093_rename_relevance_to_target"
branch_labels = None
depends_on = None

#: org-scoped tables created here — RLS is applied to each at creation.
_NEW_TABLES = (
    "ontology_domains",
    "ontology_objects",
    "ontology_links",
    "ontology_actions",
    "agent_domain_bindings",
    "coverage_states",
)


def _rls(table: str) -> None:
    """Enable FORCE RLS + the tenant_isolation policy (batch-5 fixed form).

    The bypass treats BOTH NULL and '' as unpinned — the sticky-GUC quirk
    fix (SET LOCAL leaves current_setting returning '' in later
    transactions; see scripts/apply_rls_batch5.sql).
    """
    op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def upgrade() -> None:
    op.create_table(
        "ontology_domains",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("code", sa.String(50), nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("org_id", "code", name="uq_ontology_domain_org_code"),
    )

    op.create_table(
        "ontology_objects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("domain_id", sa.String(36), nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("backing_kind", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("backing_ref", sa.String(255), nullable=True),
        sa.Column("properties_json", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("org_id", "domain_id", "name", name="uq_ontology_object"),
    )

    op.create_table(
        "ontology_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("from_object_id", sa.String(36), nullable=False, index=True),
        sa.Column("to_object_id", sa.String(36), nullable=False, index=True),
        sa.Column("link_type", sa.String(50), nullable=False),
        sa.Column("cardinality", sa.String(20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
    )

    op.create_table(
        "ontology_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("object_id", sa.String(36), nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=True),
        sa.Column("risk_level", sa.String(10), nullable=False, server_default="read"),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("params_json", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("org_id", "object_id", "name", name="uq_ontology_action"),
    )

    op.create_table(
        "agent_domain_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("agent_name", sa.String(255), nullable=False, index=True),
        sa.Column("domain_id", sa.String(36), nullable=False, index=True),
        sa.Column("permission_scope", sa.String(20), nullable=False, server_default="read"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("org_id", "agent_name", "domain_id",
                            name="uq_agent_domain_binding"),
    )

    op.create_table(
        "coverage_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("domain_id", sa.String(36), nullable=False, index=True),
        sa.Column("data_connected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("objects_live", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions_executable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agents_deployed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metric_name", sa.String(255), nullable=True),
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("org_id", "domain_id", name="uq_coverage_org_domain"),
    )

    # RLS at creation — tables are empty, no lock window needed.
    for _t in _NEW_TABLES:
        _rls(_t)


def downgrade() -> None:
    for _t in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_t}")
        op.execute(f"ALTER TABLE {_t} DISABLE ROW LEVEL SECURITY")
        op.drop_table(_t)
