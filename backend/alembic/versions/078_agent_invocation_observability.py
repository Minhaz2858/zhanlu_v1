"""Add model_name + tool_call_count to agent_invocations.

Revision ID: 078_agent_invocation_observability
Revises: 077_add_session_state
Create Date: 2026-08-29

Adds two nullable columns for the agent-observability UI:
- ``model_name`` — the LLM model id that served the turn (String 128)
- ``tool_call_count`` — number of tool calls made during the turn (Integer)

Idempotent: column-existence checks first (project convention).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "078_agent_invocation_observability"
down_revision = "077_add_session_state"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = [c["name"] for c in inspector.get_columns(table)]
    return column in cols


def upgrade() -> None:
    if not _column_exists("agent_invocations", "model_name"):
        op.add_column("agent_invocations", sa.Column("model_name", sa.String(128), nullable=True))
    if not _column_exists("agent_invocations", "tool_call_count"):
        op.add_column("agent_invocations", sa.Column("tool_call_count", sa.Integer(), nullable=True))
    # Index for the observability filters (conversation + recency).
    op.create_index(
        "ix_agent_invocations_conversation_created",
        "agent_invocations",
        ["conversation_id", "created_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_invocations_conversation_created", table_name="agent_invocations")
    if _column_exists("agent_invocations", "tool_call_count"):
        op.drop_column("agent_invocations", "tool_call_count")
    if _column_exists("agent_invocations", "model_name"):
        op.drop_column("agent_invocations", "model_name")
