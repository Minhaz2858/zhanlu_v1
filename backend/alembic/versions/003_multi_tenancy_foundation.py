"""Phase 1: Multi-tenancy foundation — org_id/app_id on all tables + Organization/AppWorkspace.

Revision ID: 003
Revises: 002
Create Date: 2025-07-12

This migration:
1. Creates ``organizations`` and ``app_workspaces`` tables
2. Adds ``org_id`` (default 'default-org') and ``app_id`` (default 'default-app')
   columns to every existing table — the multi-tenant isolation wall.
"""

from alembic import op
import sqlalchemy as sa


revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


# All tables that existed before this migration and need org_id/app_id
EXISTING_TABLES = [
    "users",
    "projects",
    "chat_sessions",
    "chat_messages",
    "agent_apps",
    "agent_conversations",
    "agent_memories",
    "agent_todos",
    "knowledge_bases",
    "automation_tasks",
    "tools",
    "user_files",
    "reports",
    "decision_flows",
    "market_agents",
    "mcp_servers",
    "user_settings",
    "analytics_events",
    "otp_codes",
    "password_reset_tokens",
]


def upgrade() -> None:
    # --- Create new multi-tenancy tables ---
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("plan", sa.String(50), server_default="free", nullable=False),
        sa.Column("settings_json", sa.JSON, nullable=True),
    )
    op.create_index("ix_organizations_org_id", "organizations", ["org_id"])
    op.create_index("ix_organizations_app_id", "organizations", ["app_id"])

    op.create_table(
        "app_workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("config_json", sa.JSON, nullable=True),
        sa.UniqueConstraint("org_id", "slug", name="uq_app_workspaces_org_slug"),
    )
    op.create_index("ix_app_workspaces_org_id", "app_workspaces", ["org_id"])
    op.create_index("ix_app_workspaces_app_id", "app_workspaces", ["app_id"])

    # --- Add org_id / app_id to every existing table ---
    for table_name in EXISTING_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False)
            )
            batch_op.add_column(
                sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False)
            )
        op.create_index(f"ix_{table_name}_org_id", table_name, ["org_id"])
        op.create_index(f"ix_{table_name}_app_id", table_name, ["app_id"])


def downgrade() -> None:
    # Remove org_id / app_id from existing tables
    for table_name in EXISTING_TABLES:
        op.drop_index(f"ix_{table_name}_app_id", table_name=table_name)
        op.drop_index(f"ix_{table_name}_org_id", table_name=table_name)
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column("app_id")
            batch_op.drop_column("org_id")

    op.drop_index("ix_app_workspaces_app_id", table_name="app_workspaces")
    op.drop_index("ix_app_workspaces_org_id", table_name="app_workspaces")
    op.drop_table("app_workspaces")

    op.drop_index("ix_organizations_app_id", table_name="organizations")
    op.drop_index("ix_organizations_org_id", table_name="organizations")
    op.drop_table("organizations")
