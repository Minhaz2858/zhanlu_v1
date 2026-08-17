"""Add role_descriptions JSON column to users.

Revision ID: 058_user_role_descriptions
Revises: 057_smart_skill_agent_columns
Create Date: 2026-08-14

Adds a single nullable JSON column ``role_descriptions`` to the ``users``
table to support role-based personalization of AI agent answers.

- ``role_descriptions``  JSON array of free-text strings (one user, many roles).

It is intentionally separate from the existing ``role`` column (which holds
the binary auth role "admin"/"user" and is checked in many places across the
codebase). Keeping them distinct means no existing auth check changes.

``sa.JSON()`` is used (not JSONB) to match the existing JSON column usage in
``users``'s sibling models and the ``JSON`` column type in ``app/models/user.py``.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "058_user_role_descriptions"
down_revision = "057_smart_skill_agent_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "role_descriptions",
            sa.JSON(),
            nullable=True,
            comment="Free-text business role descriptions (JSON array of strings); "
            "admin-edited, injected into agent system prompt for personalization",
        ),
    )


def downgrade():
    op.drop_column("users", "role_descriptions")
