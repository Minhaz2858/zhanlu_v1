"""Merge branches 071 and 072 into a single head.

Revision ID: 073_merge_071_072
Revises: 071_email_notification_settings, 072_artifacts_source
Create Date: 2026-08-21

The migration graph had two heads after 056:
  - Branch A (main): 056 → 057_smart_skill_agent_columns → 058 → … → 071
  - Branch B: 056 → 057_add_llm_model_capability_fields → 072

Alembic requires a single head to run migrations. This merge revision
joins both branches so the graph has one head again.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "073_merge_071_072"
down_revision = ("071_email_notification_settings", "072_artifacts_source")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
