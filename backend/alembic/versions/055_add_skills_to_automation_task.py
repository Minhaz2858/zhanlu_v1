"""Add skills JSON column to automation_tasks.

Revision ID: 055
Revises: 054_add_schema_to_knowledge_base
Create Date: 2026-08-13

Lets an automation task carry an ordered list of skill names that should be
made available to the agent at execution time. The executor injects a compact
metadata index (progressive disclosure) and the agent loads full SKILL.md
bodies on demand via the ``skills``/``load_skill_body`` tool.

Nullable (no server default) to match ``agent_apps.skills``; the read path
normalizes null -> [] so legacy rows behave like "no skills enabled".
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "055_add_skills_to_automation_task"
down_revision = "054_add_schema_to_knowledge_base"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "automation_tasks",
        sa.Column(
            "skills",
            sa.JSON(),
            nullable=True,
            comment="Ordered skill names enabled for this automation task",
        ),
    )


def downgrade():
    op.drop_column("automation_tasks", "skills")
