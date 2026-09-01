"""Add 12 missing P1-15 tables: skill_runs, agent_invocations, agent_test_cases,
skill_test_cases, artifact_build_manifests, datasources, metric_definitions,
semantic_mappings, context_manifests, experience_entries, learning_proposals,
audit_events.

Revision ID: 014
Revises: 013
Create Date: 2025-07-15

Tables added:
- skill_runs: execution history for skill invocations
- agent_invocations: agent invocation records with timing and result
- agent_test_cases: test cases for agent behavior verification
- skill_test_cases: test cases for skill verification
- artifact_build_manifests: build manifests for artifact generation
- datasources: datasource connection definitions
- metric_definitions: business metric definitions for NL2SQL
- semantic_mappings: semantic layer mappings for column aliasing
- context_manifests: standalone structured context records
- experience_entries: experience/learning feedback entries
- learning_proposals: proposed improvements from learned experience
- audit_events: structured audit events (complement to audit_logs)
"""

from alembic import op
import sqlalchemy as sa


revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. skill_runs ──────────────────────────────────────────────
    op.create_table(
        "skill_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("skill_profile_id", sa.String(36), nullable=True),
        sa.Column("agent_skill_binding_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        # Execution
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("artifacts_produced", sa.JSON(), nullable=True),
        # Timing
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        # Cost
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("cost_amount", sa.Float(), nullable=True),
        # Retry
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("parent_run_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_skill_runs_skill_profile_id", "skill_runs", ["skill_profile_id"])
    op.create_index("ix_skill_runs_execution_id", "skill_runs", ["execution_id"])
    op.create_index("ix_skill_runs_status", "skill_runs", ["status"])

    # ── 2. agent_invocations ───────────────────────────────────────
    op.create_table(
        "agent_invocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("agent_app_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        # Invocation type
        sa.Column("invocation_type", sa.String(30), nullable=False),
        sa.Column("trigger", sa.String(30), nullable=False, server_default="user"),
        sa.Column("input_message", sa.Text(), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=True),
        # Result
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("assistant_content", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        # Timing
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        # Cost
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("cost_amount", sa.Float(), nullable=True),
        # Tracing
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("span_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_agent_invocations_agent_app_id", "agent_invocations", ["agent_app_id"])
    op.create_index("ix_agent_invocations_execution_id", "agent_invocations", ["execution_id"])
    op.create_index("ix_agent_invocations_status", "agent_invocations", ["status"])
    op.create_index("ix_agent_invocations_trace_id", "agent_invocations", ["trace_id"])

    # ── 3. agent_test_cases ────────────────────────────────────────
    op.create_table(
        "agent_test_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("agent_app_id", sa.String(36), nullable=False),
        sa.Column("skill_profile_id", sa.String(36), nullable=True),
        # Test spec
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("test_type", sa.String(30), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("expected_output_json", sa.JSON(), nullable=True),
        sa.Column("expected_behavior", sa.Text(), nullable=True),
        sa.Column("assertions", sa.JSON(), nullable=True),
        # Execution
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_result", sa.String(20), nullable=True),
        sa.Column("last_output_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pass_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_agent_test_cases_agent_app_id", "agent_test_cases", ["agent_app_id"])
    op.create_index("ix_agent_test_cases_status", "agent_test_cases", ["status"])

    # ── 4. skill_test_cases ────────────────────────────────────────
    op.create_table(
        "skill_test_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("skill_profile_id", sa.String(36), nullable=False),
        sa.Column("skill_candidate_id", sa.String(36), nullable=True),
        # Test spec
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("test_type", sa.String(30), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("expected_output_json", sa.JSON(), nullable=True),
        sa.Column("expected_schema_valid", sa.Boolean(), nullable=True),
        sa.Column("assertions", sa.JSON(), nullable=True),
        # Execution
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sandbox_job_id", sa.String(36), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_result", sa.String(20), nullable=True),
        sa.Column("last_output_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pass_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_skill_test_cases_skill_profile_id", "skill_test_cases", ["skill_profile_id"])
    op.create_index("ix_skill_test_cases_status", "skill_test_cases", ["status"])

    # ── 5. artifact_build_manifests ─────────────────────────────────
    op.create_table(
        "artifact_build_manifests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("artifact_version_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        # Build spec
        sa.Column("build_type", sa.String(30), nullable=False),
        sa.Column("template_asset_id", sa.String(36), nullable=True),
        sa.Column("template_checksum", sa.String(64), nullable=True),
        sa.Column("build_params", sa.JSON(), nullable=True),
        sa.Column("data_snapshot_ids", sa.JSON(), nullable=True),
        # Build result
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sandbox_job_id", sa.String(36), nullable=True),
        sa.Column("output_checksum", sa.String(64), nullable=True),
        sa.Column("output_file_name", sa.String(500), nullable=True),
        sa.Column("output_size", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Timing
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        # Validation
        sa.Column("validation_report", sa.JSON(), nullable=True),
    )
    op.create_index("ix_artifact_build_manifests_artifact_id", "artifact_build_manifests", ["artifact_id"])
    op.create_index("ix_artifact_build_manifests_status", "artifact_build_manifests", ["status"])

    # ── 6. datasources ─────────────────────────────────────────────
    op.create_table(
        "datasources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # Identity
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("engine", sa.String(30), nullable=False),
        # Connection
        sa.Column("connection_config", sa.JSON(), nullable=False),
        sa.Column("connection_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("last_connected_at", sa.DateTime(), nullable=True),
        # Schema snapshot (cached)
        sa.Column("schema_snapshot", sa.JSON(), nullable=True),
        sa.Column("schema_refreshed_at", sa.DateTime(), nullable=True),
        # Controls
        sa.Column("max_rows_per_query", sa.Integer(), nullable=False, server_default=sa.text("10000")),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_datasources_name", "datasources", ["name"])
    op.create_index("ix_datasources_engine", "datasources", ["engine"])

    # ── 7. metric_definitions ──────────────────────────────────────
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # Identity
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        # Definition
        sa.Column("datasource_id", sa.String(36), nullable=False),
        sa.Column("base_sql", sa.Text(), nullable=True),
        sa.Column("base_table", sa.String(200), nullable=True),
        sa.Column("base_column", sa.String(200), nullable=True),
        sa.Column("aggregation", sa.String(30), nullable=True),
        # Semantics
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("unit", sa.String(50), nullable=True),
        sa.Column("format_pattern", sa.String(100), nullable=True),
        sa.Column("synonyms", sa.JSON(), nullable=True),
        # Governance
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_metric_definitions_datasource_id", "metric_definitions", ["datasource_id"])
    op.create_index("ix_metric_definitions_name", "metric_definitions", ["name"])

    # ── 8. semantic_mappings ───────────────────────────────────────
    op.create_table(
        "semantic_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("datasource_id", sa.String(36), nullable=False),
        sa.Column("metric_definition_id", sa.String(36), nullable=True),
        # Mapping target
        sa.Column("table_name", sa.String(200), nullable=False),
        sa.Column("column_name", sa.String(200), nullable=False),
        # Semantics
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data_type", sa.String(50), nullable=True),
        sa.Column("synonyms", sa.JSON(), nullable=True),
        # Relationship hints
        sa.Column("join_key_to", sa.String(200), nullable=True),
        sa.Column("is_primary_key", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_foreign_key", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_dimension", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_measure", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_semantic_mappings_datasource_id", "semantic_mappings", ["datasource_id"])
    op.create_index("ix_semantic_mappings_table_column", "semantic_mappings", ["table_name", "column_name"])

    # ── 9. context_manifests ───────────────────────────────────────
    op.create_table(
        "context_manifests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # Identity
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("context_type", sa.String(50), nullable=False),
        sa.Column("version", sa.String(50), nullable=False, server_default="1.0.0"),
        # Content
        sa.Column("content_json", sa.JSON(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(30), nullable=True),
        sa.Column("source_ref", sa.String(500), nullable=True),
        # Relevance
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        # Usage stats
        sa.Column("access_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_context_manifests_context_type", "context_manifests", ["context_type"])
    op.create_index("ix_context_manifests_is_active", "context_manifests", ["is_active"])

    # ── 10. experience_entries ────────────────────────────────────
    op.create_table(
        "experience_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("skill_run_id", sa.String(36), nullable=True),
        sa.Column("agent_app_id", sa.String(36), nullable=True),
        # Entry type
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("summary", sa.String(500), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        # Feedback signal
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("user_rating", sa.Integer(), nullable=True),
        sa.Column("user_feedback", sa.Text(), nullable=True),
        # Metadata
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("embedding_hash", sa.String(64), nullable=True),
    )
    op.create_index("ix_experience_entries_execution_id", "experience_entries", ["execution_id"])
    op.create_index("ix_experience_entries_outcome", "experience_entries", ["outcome"])
    op.create_index("ix_experience_entries_entry_type", "experience_entries", ["entry_type"])

    # ── 11. learning_proposals ────────────────────────────────────
    op.create_table(
        "learning_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # References
        sa.Column("experience_entry_id", sa.String(36), nullable=True),
        sa.Column("agent_app_id", sa.String(36), nullable=True),
        sa.Column("skill_profile_id", sa.String(36), nullable=True),
        # Proposal
        sa.Column("proposal_type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("expected_impact", sa.Float(), nullable=True),
        # Review
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("reviewed_by", sa.String(36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        # Application
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("applied_result", sa.String(20), nullable=True),
    )
    op.create_index("ix_learning_proposals_agent_app_id", "learning_proposals", ["agent_app_id"])
    op.create_index("ix_learning_proposals_status", "learning_proposals", ["status"])

    # ── 12. audit_events ───────────────────────────────────────────
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # Event identity
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("event_source", sa.String(50), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        # Actor
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("actor_name", sa.String(100), nullable=True),
        # Target
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=True),
        # NL2SQL-specific
        sa.Column("datasource_id", sa.String(36), nullable=True),
        sa.Column("binding_id", sa.String(36), nullable=True),
        sa.Column("sql_text_hash", sa.String(64), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("query_duration_ms", sa.Integer(), nullable=True),
        # Policy
        sa.Column("policy_decision", sa.String(20), nullable=True),
        sa.Column("policy_reasons", sa.JSON(), nullable=True),
        # Outcome
        sa.Column("outcome", sa.String(20), nullable=False, server_default="success"),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Timing
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index("ix_audit_events_trace_id", "audit_events", ["trace_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("learning_proposals")
    op.drop_table("experience_entries")
    op.drop_table("context_manifests")
    op.drop_table("semantic_mappings")
    op.drop_table("metric_definitions")
    op.drop_table("datasources")
    op.drop_table("artifact_build_manifests")
    op.drop_table("skill_test_cases")
    op.drop_table("agent_test_cases")
    op.drop_table("agent_invocations")
    op.drop_table("skill_runs")
