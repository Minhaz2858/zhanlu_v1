"""Add Smart Skill Agent columns to tools.

Revision ID: 057_smart_skill_agent_columns
Revises: 056_add_llm_models_bypass_hallucination_guardrail
Create Date: 2026-08-13

Adds three nullable JSON columns to the ``tools`` table in support of the
Kimi-equivalent Smart Skill Agent:

- ``references_manifest``  JSON dict: reference filename -> one-line summary.
- ``assets_manifest``      JSON dict: asset relative path -> one-line description.
- ``embedding``            JSON float list: semantic embedding of description+body.

All three are nullable and default to NULL so existing rows (flat-file skills)
continue to work unchanged. The actual reference/asset file bytes live on the
filesystem under ~/.zhanlu/skills/<category>/<name>/; these columns only hold
metadata for discovery + runtime progressive disclosure.

``sa.JSON()`` is used (not JSONB) to match the existing ``Tool.sources`` /
``Tool.references`` columns and the model's ``JSON`` column type.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "057_smart_skill_agent_columns"
down_revision = "056_add_llm_models_bypass_hallucination_guardrail"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tools",
        sa.Column(
            "references_manifest",
            sa.JSON(),
            nullable=True,
            comment="Map of reference filename -> one-line summary (folder-style skills)",
        ),
    )
    op.add_column(
        "tools",
        sa.Column(
            "assets_manifest",
            sa.JSON(),
            nullable=True,
            comment="Map of asset relative path -> one-line description (folder-style skills)",
        ),
    )
    op.add_column(
        "tools",
        sa.Column(
            "embedding",
            sa.JSON(),
            nullable=True,
            comment="Semantic embedding of skill description+body as a JSON float list",
        ),
    )


def downgrade():
    op.drop_column("tools", "embedding")
    op.drop_column("tools", "assets_manifest")
    op.drop_column("tools", "references_manifest")
