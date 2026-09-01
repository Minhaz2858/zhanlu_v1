"""Add is_system flag to agent_app table.

Marks platform-shipped agents (agent_builder, skill_agent,
automation_agent, general_assistant, power_user, data_agent, ...)
so the frontend can hide them from user-facing agent lists
(My Space, the agent picker, the active-agent chip) while the
runtime still uses them — in particular general_assistant is
auto-selected silently for any chat with no user-picked agent.

data_agent is NOT a row in agent_apps (it lives in BUILTIN_AGENTS
and is invoked via ask_data_agent), so this migration is a no-op
for it; the rest are seeded by ensure_system_agents() and get
is_system=True on every startup backfill.

Revision ID: 026
Revises: 025
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


# Names of the platform-shipped agents. The migration back-fills
# is_system=True on these rows so the new column is consistent with
# ``ensure_system_agents()`` even for databases that pre-date this
# migration. New rows created by the app code path will set the
# field directly on insert.
SYSTEM_AGENT_NAMES = (
    "agent_builder",
    "skill_agent",
    "automation_agent",
    "general_assistant",
    "power_user",
)


def upgrade() -> None:
    # Add the column. Use a server-side default of False so existing
    # rows (which are all user-created at the moment of migration)
    # default to is_system=False without a separate UPDATE pass.
    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_system",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        batch_op.create_index(
            "ix_agent_apps_is_system",
            ["is_system"],
        )

    # Back-fill system-agent rows so the runtime / UI agree even
    # before ensure_system_agents() runs at next startup.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent_apps SET is_system = TRUE "
            "WHERE name IN :names AND is_deleted = FALSE"
        ).bindparams(
            # SQLAlchemy expanding bind — see SA docs.
            sa.bindparam("names", expanding=True),
        ),
        {"names": list(SYSTEM_AGENT_NAMES)},
    )


def downgrade() -> None:
    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.drop_index("ix_agent_apps_is_system")
        batch_op.drop_column("is_system")
