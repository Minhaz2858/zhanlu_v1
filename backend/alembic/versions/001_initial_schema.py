"""Initial schema — all 14 entity tables + agent_conversations

Revision ID: 001
Revises:
Create Date: 2025-07-10

Creates all tables matching the Base44 entity schemas.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Helper: shared columns for all entity tables
    def base_columns():
        return [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_date", sa.DateTime, nullable=True),
            sa.Column("updated_date", sa.DateTime, nullable=True),
            sa.Column("created_by_id", sa.String(36), nullable=True),
            sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        ]

    # --- users ---
    op.create_table("users", *base_columns(),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(50), nullable=False, server_default="user"),
        sa.Column("password_hash", sa.String(255), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # --- projects ---
    op.create_table("projects", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("color", sa.String(50), nullable=True),
        sa.Column("status", sa.String(50), nullable=True, server_default="active"),
    )

    # --- chat_sessions ---
    op.create_table("chat_sessions", *base_columns(),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("starred", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("last_message_at", sa.String(100), nullable=True),
    )

    # --- chat_messages ---
    op.create_table("chat_messages", *base_columns(),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("order", sa.Integer, nullable=True),
        sa.Column("trace", sa.JSON, nullable=True),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])

    # --- agent_apps ---
    op.create_table("agent_apps", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("project", sa.String(255), nullable=True, server_default="global"),
        sa.Column("capabilities", sa.JSON, nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("agent_type", sa.String(50), nullable=True, server_default="sequential"),
        sa.Column("prompt_identity", sa.Text, nullable=True),
        sa.Column("prompt_boundary", sa.Text, nullable=True),
        sa.Column("prompt_reasoning", sa.Text, nullable=True),
        sa.Column("prompt_tools", sa.Text, nullable=True),
        sa.Column("prompt_output", sa.Text, nullable=True),
        sa.Column("skills", sa.JSON, nullable=True),
        sa.Column("knowledge_bases", sa.JSON, nullable=True),
        sa.Column("topology", sa.String(50), nullable=True, server_default="standalone"),
        sa.Column("sub_agents", sa.JSON, nullable=True),
        sa.Column("flow_mode", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("flow", sa.JSON, nullable=True),
        sa.Column("max_call_count", sa.Integer, nullable=True),
        sa.Column("max_retries", sa.Integer, nullable=True),
        sa.Column("max_iterations", sa.Integer, nullable=True),
        sa.Column("data_read", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("data_write", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("human_fallback", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("trace_enabled", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("log_level", sa.String(20), nullable=True, server_default="info"),
        sa.Column("temperature", sa.Float, nullable=True),
        sa.Column("top_p", sa.Float, nullable=True),
        sa.Column("max_tokens", sa.Integer, nullable=True),
        sa.Column("status", sa.String(50), nullable=True, server_default="draft"),
        sa.Column("tool_config", sa.JSON, nullable=True),
    )

    # --- knowledge_bases ---
    op.create_table("knowledge_bases", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=True, server_default="global"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("type", sa.String(50), nullable=True),
        sa.Column("source_kind", sa.String(50), nullable=True),
        sa.Column("db_type", sa.String(100), nullable=True),
        sa.Column("host", sa.String(255), nullable=True),
        sa.Column("port", sa.Integer, nullable=True),
        sa.Column("database_name", sa.String(255), nullable=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password", sa.String(255), nullable=True),
        sa.Column("api_url", sa.String(500), nullable=True),
        sa.Column("file_type", sa.String(100), nullable=True),
        sa.Column("file_url", sa.String(500), nullable=True),
        sa.Column("item_count", sa.Integer, nullable=True, server_default="0"),
        sa.Column("status", sa.String(50), nullable=True, server_default="active"),
    )

    # --- automation_tasks ---
    op.create_table("automation_tasks", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=True, server_default="global"),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("schedule", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), nullable=True, server_default="paused"),
        sa.Column("last_run", sa.String(100), nullable=True),
        sa.Column("last_result", sa.Text, nullable=True),
        sa.Column("execution_history", sa.JSON, nullable=True),
    )

    # --- tools ---
    op.create_table("tools", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("kind", sa.String(50), nullable=False, server_default="custom_tool"),
        sa.Column("trigger", sa.String(255), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=True, server_default=sa.text("true")),
        sa.Column("call_count", sa.Integer, nullable=True, server_default="0"),
        sa.Column("status", sa.String(50), nullable=True, server_default="active"),
        sa.Column("source", sa.String(50), nullable=True, server_default="builtin"),
        sa.Column("github_url", sa.String(500), nullable=True),
        sa.Column("skill_file_url", sa.String(500), nullable=True),
        sa.Column("version", sa.String(50), nullable=True),
        sa.Column("license", sa.String(100), nullable=True),
        sa.Column("platform", sa.String(100), nullable=True),
        sa.Column("skill_id", sa.Integer, nullable=True),
        sa.Column("publisher", sa.String(255), nullable=True),
        sa.Column("skill_md", sa.Text, nullable=True),
        sa.Column("sources", sa.JSON, nullable=True),
        sa.Column("references", sa.JSON, nullable=True),
    )

    # --- user_files ---
    op.create_table("user_files", *base_columns(),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("file_type", sa.String(100), nullable=True),
        sa.Column("size", sa.Integer, nullable=True),
        sa.Column("file_url", sa.String(500), nullable=True),
        sa.Column("source", sa.String(50), nullable=True, server_default="upload"),
        sa.Column("resource_kind", sa.String(100), nullable=True),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("agent_name", sa.String(255), nullable=True),
        sa.Column("slides", sa.JSON, nullable=True),
        sa.Column("read", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("pinned", sa.Boolean, nullable=True, server_default=sa.text("false")),
    )
    op.create_index("ix_user_files_session_id", "user_files", ["session_id"])

    # --- reports ---
    op.create_table("reports", *base_columns(),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("type", sa.String(100), nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("file_url", sa.String(500), nullable=True),
        sa.Column("status", sa.String(50), nullable=True, server_default="generating"),
    )

    # --- decision_flows ---
    op.create_table("decision_flows", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("steps", sa.Integer, nullable=True),
        sa.Column("status", sa.String(50), nullable=True, server_default="draft"),
    )

    # --- market_agents ---
    op.create_table("market_agents", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("capabilities", sa.JSON, nullable=True),
        sa.Column("rating", sa.Float, nullable=True, server_default="0"),
        sa.Column("subscribers", sa.Integer, nullable=True, server_default="0"),
    )

    # --- mcp_servers ---
    op.create_table("mcp_servers", *base_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("server_url", sa.String(500), nullable=False),
        sa.Column("transport", sa.String(50), nullable=True, server_default="streamable"),
        sa.Column("status", sa.String(50), nullable=True, server_default="disconnected"),
        sa.Column("tools_count", sa.Integer, nullable=True, server_default="0"),
        sa.Column("resources_count", sa.Integer, nullable=True, server_default="0"),
    )

    # --- user_settings ---
    op.create_table("user_settings", *base_columns(),
        sa.Column("language", sa.String(10), nullable=True, server_default="zh"),
        sa.Column("timezone", sa.String(100), nullable=True),
        sa.Column("date_format", sa.String(20), nullable=True, server_default="YYYY-MM-DD"),
        sa.Column("context_length", sa.Integer, nullable=True),
        sa.Column("default_model", sa.String(255), nullable=True),
        sa.Column("fallback_model", sa.String(255), nullable=True),
        sa.Column("max_tokens", sa.Integer, nullable=True),
        sa.Column("temperature", sa.Float, nullable=True),
        sa.Column("file_upload_enabled", sa.Boolean, nullable=True, server_default=sa.text("true")),
        sa.Column("session_retention_days", sa.Integer, nullable=True),
        sa.Column("voice_interaction_enabled", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("voice_language", sa.String(20), nullable=True),
        sa.Column("sso_enabled", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("mfa_required", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("rbac_mode", sa.String(20), nullable=True, server_default="flat"),
        sa.Column("session_timeout_minutes", sa.Integer, nullable=True),
        sa.Column("password_min_length", sa.Integer, nullable=True),
        sa.Column("compute_quota", sa.Integer, nullable=True),
        sa.Column("kb_storage_limit", sa.Integer, nullable=True),
        sa.Column("agent_call_limit", sa.Integer, nullable=True),
        sa.Column("task_count_limit", sa.Integer, nullable=True),
        sa.Column("gpu_quota", sa.Integer, nullable=True),
        sa.Column("api_rate_limit", sa.Integer, nullable=True),
        sa.Column("data_masking", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("pii_detection", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("encryption_enabled", sa.Boolean, nullable=True, server_default=sa.text("true")),
        sa.Column("third_party_whitelist", sa.String(1000), nullable=True),
        sa.Column("ip_whitelist", sa.String(1000), nullable=True),
        sa.Column("audit_logging", sa.Boolean, nullable=True, server_default=sa.text("true")),
        sa.Column("audit_retention_days", sa.Integer, nullable=True),
        sa.Column("compliance_mode", sa.String(20), nullable=True, server_default="standard"),
        sa.Column("data_residency", sa.String(100), nullable=True),
        sa.Column("data_quality_check", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("lineage_tracking", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("data_retention_policy", sa.String(500), nullable=True),
        sa.Column("monitoring_enabled", sa.Boolean, nullable=True, server_default=sa.text("true")),
        sa.Column("log_level", sa.String(20), nullable=True, server_default="info"),
        sa.Column("tracing_enabled", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("alerting_enabled", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("monthly_budget", sa.Float, nullable=True),
        sa.Column("cost_alert_threshold", sa.Float, nullable=True),
        sa.Column("usage_analytics", sa.Boolean, nullable=True, server_default=sa.text("true")),
        sa.Column("api_key_rotation_days", sa.Integer, nullable=True),
        sa.Column("webhook_url", sa.String(500), nullable=True),
        sa.Column("sdk_access_enabled", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("cluster_region", sa.String(100), nullable=True),
        sa.Column("auto_scaling", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("maintenance_window", sa.String(255), nullable=True),
        sa.Column("backup_schedule", sa.String(255), nullable=True),
    )

    # --- agent_conversations ---
    op.create_table("agent_conversations", *base_columns(),
        sa.Column("agent_name", sa.String(255), nullable=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("messages", sa.JSON, nullable=True),
        sa.Column("status", sa.String(50), nullable=True, server_default="active"),
        sa.Column("metadata", sa.JSON, nullable=True),
    )

    # --- agent_memories ---
    op.create_table("agent_memories", *base_columns(),
        sa.Column("agent_app_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("target", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("char_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("importance", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ttl_days", sa.Integer, nullable=True),
        sa.Column("usage_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_agent_memories_agent_app_id", "agent_memories", ["agent_app_id"])
    op.create_index("ix_agent_memories_user_id", "agent_memories", ["user_id"])
    op.create_index("ix_agent_memories_content_hash", "agent_memories", ["content_hash"])

    # --- agent_todos ---
    op.create_table("agent_todos", *base_columns(),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("agent_app_id", sa.String(36), nullable=True),
        sa.Column("items", sa.JSON, nullable=False),
    )
    op.create_index("ix_agent_todos_conversation_id", "agent_todos", ["conversation_id"])
    op.create_index("ix_agent_todos_agent_app_id", "agent_todos", ["agent_app_id"])

    # --- analytics_events ---
    op.create_table("analytics_events", *base_columns(),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.Text, nullable=True),
    )
    op.create_index("ix_analytics_events_event_type", "analytics_events", ["event_type"])
    op.create_index("ix_analytics_events_user_id", "analytics_events", ["user_id"])

    # --- otp_codes ---
    op.create_table("otp_codes", *base_columns(),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_otp_codes_email", "otp_codes", ["email"])

    # --- password_reset_tokens ---
    op.create_table("password_reset_tokens", *base_columns(),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("token", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.create_index("ix_password_reset_tokens_token", "password_reset_tokens", ["token"], unique=True)


def downgrade() -> None:
    for table in [
        "password_reset_tokens", "otp_codes", "analytics_events",
        "agent_todos", "agent_memories",
        "agent_conversations", "user_settings", "mcp_servers", "market_agents",
        "decision_flows", "reports", "user_files", "tools", "automation_tasks",
        "knowledge_bases", "agent_apps", "chat_messages", "chat_sessions",
        "projects", "users",
    ]:
        op.drop_table(table)
