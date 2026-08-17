"""Add bypass_hallucination_guardrail flag to llm_models.

Revision ID: 056
Revises: 055_add_skills_to_automation_task
Create Date: 2026-08-13

Lets an admin flag a specific LLM endpoint as "bypass the hallucination
guardrail". Weak local models (e.g. qwen3.5-27b via Ollama) that cannot
reliably call ``ask_data_agent`` can answer data questions directly instead
of hitting the MAX_GUARDRAIL_RETRIES fallback. The flag is per-row so new
local models added in the future can opt in with a single boolean toggle —
no code change required.

Default False keeps the guardrail active for all existing endpoints
(deepseek, gpt-4, etc.).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "056_add_llm_models_bypass_hallucination_guardrail"
down_revision = "055_add_skills_to_automation_task"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "llm_models",
        sa.Column(
            "bypass_hallucination_guardrail",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="When True, skip the hallucination guardrail (ask_data_agent force) for this model",
        ),
    )


def downgrade():
    op.drop_column("llm_models", "bypass_hallucination_guardrail")
