"""125_agent_run_provenance — canonical prov-v1 envelope column on agent_runs.

Revision ID: 125_agent_run_provenance
Revises: 124_agent_binding_enable
Create Date: 2026-11-03

WHY
---
Phase 8 T8-8: every agent execution gets a canonical provenance envelope
written by the single terminal choke point (AgentRunService.finalize_run).
Nullable Text column — historical rows stay NULL and readable; the
tenant RLS policy is applied (the table predates the RLS rollout).

Safe on production data: no backfill, no existing column touched.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "125_agent_run_provenance"
down_revision: Union[str, Sequence[str], None] = "124_agent_binding_enable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("provenance_json", sa.Text(), nullable=True),
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_runs")
    op.execute("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON agent_runs USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_runs")
    op.drop_column("agent_runs", "provenance_json")
