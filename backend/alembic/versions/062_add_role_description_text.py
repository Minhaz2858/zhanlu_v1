"""Add role_description_text TEXT column to users.

Revision ID: 062_add_role_description_text
Revises: 061_add_pinned_to_reports_and_automation_files
Create Date: 2026-08-14

Adds a nullable ``role_description_text`` TEXT column to ``users`` to hold the
AI-generated prose description for the user's business role(s). It is
intentionally separate from ``role_descriptions`` (a JSON array of keyword
strings) so existing consumers that read ``role_descriptions`` as ``list[str]``
are unaffected.

Flow: admin saves Business Role keywords → backend async-generates a prose
description via LLM → writes it here → admin reopens Edit User to review/edit.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "062_add_role_description_text"
down_revision = "061_add_pinned_to_reports_and_automation_files"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "role_description_text",
            sa.Text(),
            nullable=True,
            comment="AI-generated prose description of the user's business role(s); "
            "admin-editable, injected into the agent system prompt for personalization",
        ),
    )


def downgrade():
    op.drop_column("users", "role_description_text")
