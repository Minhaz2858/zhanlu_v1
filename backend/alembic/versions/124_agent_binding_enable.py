"""124_agent_binding_enable — binding uniqueness, enable state, tenant RLS.

Revision ID: 124_agent_binding_enable
Revises: 123_enterprise_onboarding_runs
Create Date: 2026-11-03

WHY
---
Phase 8 ADR-1: agent-datasource bindings become a governed resource.
1. ``is_enabled`` adds a kill-switch so an agent's data access can be
   revoked instantly without destroying binding configuration.
2. A partial unique index guarantees ONE live binding per
   (agent_app_id, datasource_id) — soft-deleted rows are excluded, so a
   revoked binding can be re-created later.
3. The standard tenant RLS policy is applied (the table predates the RLS
   rollout and had none) so binding rows are org-isolated like every
   other enterprise table.

Safe on production data: the added column defaults to enabled (``true``)
and the index only constrains future writes.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "124_agent_binding_enable"
down_revision: Union[str, Sequence[str], None] = "123_enterprise_onboarding_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_data_bindings",
        sa.Column("is_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
    )
    op.create_index(
        "uq_agent_data_bindings_agent_datasource",
        "agent_data_bindings",
        ["agent_app_id", "datasource_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    # Same tenant policy as every other table (see 102_turn_identity /
    # 119_business_rule). DROP-first keeps re-runs idempotent.
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_data_bindings")
    op.execute("ALTER TABLE agent_data_bindings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_data_bindings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON agent_data_bindings USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_data_bindings")
    op.drop_index("uq_agent_data_bindings_agent_datasource",
                  table_name="agent_data_bindings")
    op.drop_column("agent_data_bindings", "is_enabled")
