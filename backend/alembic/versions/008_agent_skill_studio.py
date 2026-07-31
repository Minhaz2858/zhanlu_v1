"""Phase 6: Agent/Skill Studio — data bindings, skill bindings, skill profiles, skill candidates.

Revision ID: 008
Revises: 007
Create Date: 2025-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- agent_data_bindings ---
    op.create_table(
        "agent_data_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("agent_app_id", sa.String(36), sa.ForeignKey("agent_apps.id"), nullable=False),
        sa.Column("datasource_id", sa.String(36), nullable=False),
        sa.Column("access_mode", sa.String(20), server_default="read_only", nullable=False),
        sa.Column("allowed_tables", sa.JSON, nullable=True),
        sa.Column("allowed_columns", sa.JSON, nullable=True),
        sa.Column("blocked_tables", sa.JSON, nullable=True),
        sa.Column("row_filters", sa.JSON, nullable=True),
        sa.Column("nl2sql_enabled", sa.Boolean, server_default=sa.text("true"), nullable=False),
    )
    op.create_index("ix_agent_data_bindings_org_id", "agent_data_bindings", ["org_id"])
    op.create_index("ix_agent_data_bindings_app_id", "agent_data_bindings", ["app_id"])
    op.create_index("ix_agent_data_bindings_agent_app_id", "agent_data_bindings", ["agent_app_id"])
    op.create_index("ix_agent_data_bindings_datasource_id", "agent_data_bindings", ["datasource_id"])

    # --- agent_skill_bindings ---
    op.create_table(
        "agent_skill_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("agent_app_id", sa.String(36), sa.ForeignKey("agent_apps.id"), nullable=False),
        sa.Column("skill_name", sa.String(100), nullable=False),
        sa.Column("skill_version", sa.String(50), nullable=True),
        sa.Column("is_allowed", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("is_pinned", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("config_override", sa.JSON, nullable=True),
        sa.Column("call_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("last_used_at", sa.String(50), nullable=True),
    )
    op.create_index("ix_agent_skill_bindings_org_id", "agent_skill_bindings", ["org_id"])
    op.create_index("ix_agent_skill_bindings_app_id", "agent_skill_bindings", ["app_id"])
    op.create_index("ix_agent_skill_bindings_agent_app_id", "agent_skill_bindings", ["agent_app_id"])

    # --- skill_profiles ---
    op.create_table(
        "skill_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("tool_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("version", sa.String(50), server_default="1.0.0", nullable=False),
        sa.Column("package_path", sa.String(500), nullable=True),
        sa.Column("manifest", sa.JSON, nullable=True),
        sa.Column("skill_md", sa.Text, nullable=True),
        sa.Column("input_schema", sa.JSON, nullable=True),
        sa.Column("output_schema", sa.JSON, nullable=True),
        sa.Column("review_status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("trust_level", sa.String(20), server_default="untrusted", nullable=False),
        sa.Column("review_notes", sa.Text, nullable=True),
        sa.Column("reviewed_by", sa.String(36), nullable=True),
        sa.Column("reviewed_at", sa.String(50), nullable=True),
        sa.Column("artifact_types", sa.JSON, nullable=True),
        sa.Column("requires_sandbox", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("sandbox_image", sa.String(200), nullable=True),
        sa.Column("validators", sa.JSON, nullable=True),
        sa.Column("test_cases", sa.JSON, nullable=True),
        sa.Column("usage_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("success_rate", sa.Float, nullable=True),
    )
    op.create_index("ix_skill_profiles_org_id", "skill_profiles", ["org_id"])
    op.create_index("ix_skill_profiles_app_id", "skill_profiles", ["app_id"])
    op.create_index("ix_skill_profiles_name", "skill_profiles", ["name"])
    op.create_index("ix_skill_profiles_tool_id", "skill_profiles", ["tool_id"])

    # --- skill_candidates ---
    op.create_table(
        "skill_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_data", sa.JSON, nullable=True),
        sa.Column("generated_code", sa.Text, nullable=True),
        sa.Column("generated_manifest", sa.JSON, nullable=True),
        sa.Column("generated_skill_md", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), server_default="quarantined", nullable=False),
        sa.Column("review_notes", sa.Text, nullable=True),
        sa.Column("reviewed_by", sa.String(36), nullable=True),
        sa.Column("test_results", sa.JSON, nullable=True),
        sa.Column("sandbox_job_id", sa.String(36), nullable=True),
        sa.Column("published_skill_profile_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_skill_candidates_org_id", "skill_candidates", ["org_id"])
    op.create_index("ix_skill_candidates_app_id", "skill_candidates", ["app_id"])

    # --- Add enterprise columns to agent_apps ---
    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.add_column(sa.Column("manifest_json", sa.JSON, nullable=True))
        batch_op.add_column(sa.Column("data_bindings", sa.JSON, nullable=True))
        batch_op.add_column(sa.Column("skill_bindings", sa.JSON, nullable=True))
        batch_op.add_column(sa.Column("memory_scope", sa.String(30), server_default="user_only", nullable=True))
        batch_op.add_column(sa.Column("policy_profile", sa.JSON, nullable=True))
        batch_op.add_column(sa.Column("output_contract", sa.JSON, nullable=True))
        batch_op.add_column(sa.Column("evaluation_profile", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.drop_column("evaluation_profile")
        batch_op.drop_column("output_contract")
        batch_op.drop_column("policy_profile")
        batch_op.drop_column("memory_scope")
        batch_op.drop_column("skill_bindings")
        batch_op.drop_column("data_bindings")
        batch_op.drop_column("manifest_json")

    op.drop_table("skill_candidates")
    op.drop_table("skill_profiles")
    op.drop_table("agent_skill_bindings")
    op.drop_table("agent_data_bindings")
