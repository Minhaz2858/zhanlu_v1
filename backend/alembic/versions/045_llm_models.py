"""045 — llm_models catalog + FK columns on projects / agent_apps.

Revision ID: 045
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "045_llm_models"
down_revision = "044_agent_run_steps_and_reasoning"
branch_labels = None
depends_on = None


def upgrade():
    # 1) llm_models catalog — admin-managed LLM provider registry
    op.create_table(
        "llm_models",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("model_id", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_llm_models_org_app", "llm_models", ["org_id", "app_id"])
    op.create_index("ix_llm_models_enabled_default", "llm_models", ["enabled", "is_default"])

    # 2) FK on projects → llm_models
    op.add_column(
        "projects",
        sa.Column("llm_model_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_projects_llm_model_id", "projects", ["llm_model_id"])
    op.create_foreign_key(
        "fk_projects_llm_model_id",
        "projects", "llm_models",
        ["llm_model_id"], ["id"],
        ondelete="SET NULL",
    )

    # 3) FK on agent_apps → llm_models
    op.add_column(
        "agent_apps",
        sa.Column("llm_model_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_agent_apps_llm_model_id", "agent_apps", ["llm_model_id"])
    op.create_foreign_key(
        "fk_agent_apps_llm_model_id",
        "agent_apps", "llm_models",
        ["llm_model_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    # 3) Drop FK + column on agent_apps
    op.drop_constraint("fk_agent_apps_llm_model_id", "agent_apps", type_="foreignkey")
    op.drop_index("ix_agent_apps_llm_model_id", table_name="agent_apps")
    op.drop_column("agent_apps", "llm_model_id")

    # 2) Drop FK + column on projects
    op.drop_constraint("fk_projects_llm_model_id", "projects", type_="foreignkey")
    op.drop_index("ix_projects_llm_model_id", table_name="projects")
    op.drop_column("projects", "llm_model_id")

    # 1) Drop llm_models table
    op.drop_index("ix_llm_models_enabled_default", table_name="llm_models")
    op.drop_index("ix_llm_models_org_app", table_name="llm_models")
    op.drop_table("llm_models")
