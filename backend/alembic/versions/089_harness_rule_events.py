"""harness_rule_events — Phase 0 evidence for the Agent Harness migration.

Revision ID: 089_harness_rule_events
Revises: 088_domain_discovery
Create Date: 2026-09-18

Adds ``harness_rule_events``: one row per firing of a deterministic decision
function (should_force_create_file / detect_file_intent / is_pptx_request …).
Read it to decide which hardcoded rules are provably dead before deleting them
(see docs/plans/2026-09-18-agent-harness-migration.md).

Idempotent: existence-checked first.
"""

from alembic import op
import sqlalchemy as sa


revision = "089_harness_rule_events"
down_revision = "088_domain_discovery"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _inspector()
    if "harness_rule_events" in insp.get_table_names():
        return

    op.create_table(
        "harness_rule_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rule_name", sa.String(160), nullable=False),
        sa.Column("fired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("decision", sa.String(300), nullable=True),
        sa.Column("forced_target", sa.String(80), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("turn_id", sa.String(36), nullable=True),
        sa.Column("agent_name", sa.String(160), nullable=True),
        sa.Column("surface", sa.String(80), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false()),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("app_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_harness_rule_events_rule_name", "harness_rule_events", ["rule_name"]
    )
    op.create_index(
        "ix_harness_rule_events_conversation_id",
        "harness_rule_events",
        ["conversation_id"],
    )


def downgrade() -> None:
    insp = _inspector()
    if "harness_rule_events" in insp.get_table_names():
        op.drop_table("harness_rule_events")
