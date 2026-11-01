"""123_enterprise_onboarding_runs — onboarding lifecycle records.

Revision ID: 123_enterprise_onboarding_runs
Revises: 122_observation_linkage
Create Date: 2026-11-01

WHY
---
Phase 4: automated, evidence-driven resource onboarding needs an honest,
retryable lifecycle record per attempt (per-step status, inventory
digest for drift detection, bounded failure surface).

CHANGE
------
NEW table ``enterprise_onboarding_runs`` — no changes to existing
tables. Proposals created during onboarding remain PROPOSED.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "123_enterprise_onboarding_runs"
down_revision: Union[str, Sequence[str], None] = "122_observation_linkage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "enterprise_onboarding_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.String(length=120), nullable=False),
        sa.Column("resource_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("steps_json", sa.JSON(), nullable=True),
        sa.Column("inventory_digest", sa.String(length=64), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("is_deleted", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_enterprise_onboarding_runs_org_id",
                    "enterprise_onboarding_runs", ["org_id"])
    op.create_index("ix_enterprise_onboarding_runs_status",
                    "enterprise_onboarding_runs", ["status"])
    op.create_index("ix_eonboarding_resource",
                    "enterprise_onboarding_runs",
                    ["org_id", "resource_type", "resource_id"])


def downgrade() -> None:
    op.drop_index("ix_eonboarding_resource",
                  table_name="enterprise_onboarding_runs")
    op.drop_index("ix_enterprise_onboarding_runs_status",
                  table_name="enterprise_onboarding_runs")
    op.drop_index("ix_enterprise_onboarding_runs_org_id",
                  table_name="enterprise_onboarding_runs")
    op.drop_table("enterprise_onboarding_runs")
