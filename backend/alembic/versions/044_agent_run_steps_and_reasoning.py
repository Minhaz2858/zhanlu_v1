"""P2 — agent_run_steps table + reasoning_content on chat_messages.

Revision ID: 044
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "044_agent_run_steps_and_reasoning"
down_revision = "043_agent_runs"
branch_labels = None
depends_on = None


def upgrade():
    # 1) agent_run_steps — one row per LLM call / tool call inside a run
    op.create_table(
        "agent_run_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("step_id", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("step_type", sa.String(20), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("messages_snapshot", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.String(255), nullable=True),
        sa.Column("tool_args", sa.Text(), nullable=True),
        sa.Column("result_preview", sa.Text(), nullable=True),
        sa.Column("iteration", sa.Integer(), server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_run_steps_step_id", "agent_run_steps", ["step_id"], unique=True)
    op.create_index("ix_agent_run_steps_run_id", "agent_run_steps", ["run_id"])

    # 2) reasoning_content — persist chain-of-thought across refreshes
    op.add_column(
        "chat_messages",
        sa.Column("reasoning_content", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("chat_messages", "reasoning_content")
    op.drop_index("ix_agent_run_steps_run_id", table_name="agent_run_steps")
    op.drop_index("ix_agent_run_steps_step_id", table_name="agent_run_steps")
    op.drop_table("agent_run_steps")
