"""Add per-model capability fields to llm_models.

Revision ID: 057
Revises: 056_add_llm_models_bypass_hallucination_guardrail
Create Date: 2026-08-21

Lets an admin configure each model's capabilities from the admin UI instead
of relying on global env caps:

  * ``context_window`` (int, nullable) — real token context window. Self-hosted
    vLLM models (e.g. qwen3.6-27b-awq4) report a much smaller ``max_model_len``
    than their model card claims; without this the compaction service assumed
    128k for "qwen*" and caused context_overflow 400s.
  * ``max_output_tokens`` (int, nullable) — per-model output cap. Replaces the
    global ``LLM_MAX_TOKENS_HARD_CAP`` hack so DeepSeek (8192) is not clamped
    down to a small global value.
  * ``supports_structured_tool_calls`` (bool, default True) — False for vLLM
    served WITHOUT ``--enable-auto-tool-choice`` (tool calls arrive as XML in
    the content field, not the structured ``tool_calls`` field). When False the
    server proactively omits ``tool_choice`` and parses tool calls from content.

All columns are nullable/optional with safe defaults so existing rows are
unaffected. No data loss on (re)application.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "057_add_llm_model_capability_fields"
down_revision = "071_email_notification_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "llm_models",
        sa.Column(
            "context_window",
            sa.Integer(),
            nullable=True,
            comment="Model context window in tokens; NULL = use heuristic default",
        ),
    )
    op.add_column(
        "llm_models",
        sa.Column(
            "max_output_tokens",
            sa.Integer(),
            nullable=True,
            comment="Per-model output token cap; NULL = use global hard cap",
        ),
    )
    op.add_column(
        "llm_models",
        sa.Column(
            "supports_structured_tool_calls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="False for vLLM without --enable-auto-tool-choice",
        ),
    )


def downgrade():
    op.drop_column("llm_models", "supports_structured_tool_calls")
    op.drop_column("llm_models", "max_output_tokens")
    op.drop_column("llm_models", "context_window")
