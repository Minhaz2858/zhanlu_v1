"""Back-fill is_system=TRUE on ecisco_bi_assistant rows.

The agent is now a silent platform system agent — see
app/services/system_agents.py for the rationale. This migration is
data-only and idempotent: it sets is_system=TRUE on every live
(soft-deleted=False) row whose name is 'ecisco_bi_assistant', closing
the window between this commit landing and the next backend restart
(where ensure_system_agents() would do the same back-fill anyway).

The seeder's own backfill is the source of truth in steady state; this
migration is belt-and-suspenders so the agent is hidden from the UI
immediately after deploy, without waiting for a backend restart.

Revision ID: 042_ecisco_bi_silent_agent
Revises: 041_forecast_mlops_hitl
Create Date: 2026-08-05
"""
from alembic import op  # type: ignore[import-untyped]
import sqlalchemy as sa  # type: ignore[import-untyped]


revision = "042_ecisco_bi_silent_agent"
down_revision = "041_forecast_mlops_hitl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent_apps SET is_system = TRUE "
            "WHERE name = 'ecisco_bi_assistant' AND is_deleted = FALSE"
        )
    )


def downgrade() -> None:
    # Walk the flag back to its previous value (False) so the agent
    # becomes user-facing again. Downgrade is intentionally narrow —
    # only ecisco_bi_assistant is touched, so the other system
    # agents stamped by migration 026 are unaffected.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent_apps SET is_system = FALSE "
            "WHERE name = 'ecisco_bi_assistant' AND is_deleted = FALSE"
        )
    )
