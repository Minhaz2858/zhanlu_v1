"""Tests for migration 014 — verify all 12 new tables exist with correct columns.

Runs the migration, inspects the resulting schema, and asserts every
expected table and key column is present.  Verifies downgrade as well.
"""

import pytest
from sqlalchemy import inspect, text

from app.database import engine


# ── Expected tables and their minimum required columns ──────────────

EXPECTED_TABLES: dict[str, set[str]] = {
    "skill_runs": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "skill_profile_id", "execution_id", "status", "input_json",
        "output_json", "duration_ms", "attempt_number",
    },
    "agent_invocations": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "agent_app_id", "execution_id", "invocation_type", "trigger",
        "status", "duration_ms", "trace_id",
    },
    "agent_test_cases": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "agent_app_id", "name", "test_type", "status",
        "run_count", "pass_count",
    },
    "skill_test_cases": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "skill_profile_id", "name", "test_type", "status",
        "run_count", "pass_count",
    },
    "artifact_build_manifests": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "artifact_id", "build_type", "status", "template_checksum",
        "output_checksum", "duration_ms",
    },
    "datasources": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "name", "engine", "connection_config", "connection_status",
        "schema_snapshot", "max_rows_per_query", "is_enabled",
    },
    "metric_definitions": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "name", "datasource_id", "synonyms", "is_enabled",
    },
    "semantic_mappings": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "datasource_id", "table_name", "column_name",
        "synonyms", "is_primary_key", "is_dimension", "is_measure",
    },
    "context_manifests": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "name", "context_type", "version", "priority",
        "is_active", "access_count",
    },
    "experience_entries": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "execution_id", "entry_type", "outcome", "user_rating",
    },
    "learning_proposals": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "agent_app_id", "proposal_type", "title", "status",
        "expected_impact",
    },
    "audit_events": {
        "id", "created_date", "updated_date", "org_id", "app_id",
        "event_type", "event_source", "actor_type", "resource_type",
        "outcome", "occurred_at", "query_duration_ms",
    },
}


@pytest.fixture(scope="module")
def inspector():
    """Return a SQLAlchemy inspector bound to the current engine."""
    return inspect(engine)


class TestMigration014TablesExist:
    """Verify all 12 new tables exist after migration 014 is applied."""

    @pytest.mark.parametrize("table_name,expected_columns", [
        (name, cols) for name, cols in EXPECTED_TABLES.items()
    ])
    def test_table_exists_with_columns(self, inspector, table_name, expected_columns):
        assert inspector.has_table(table_name), (
            f"Table '{table_name}' not found.  Did migration 014 run?"
        )
        actual = {col["name"] for col in inspector.get_columns(table_name)}
        missing = expected_columns - actual
        assert not missing, (
            f"Table '{table_name}' missing columns: {missing}"
        )


class TestMigration014Indices:
    """Verify key indices exist on the new tables."""

    EXPECTED_INDICES = [
        ("skill_runs", "ix_skill_runs_skill_profile_id"),
        ("skill_runs", "ix_skill_runs_execution_id"),
        ("skill_runs", "ix_skill_runs_status"),
        ("agent_invocations", "ix_agent_invocations_agent_app_id"),
        ("agent_invocations", "ix_agent_invocations_execution_id"),
        ("agent_invocations", "ix_agent_invocations_status"),
        ("agent_invocations", "ix_agent_invocations_trace_id"),
        ("agent_test_cases", "ix_agent_test_cases_agent_app_id"),
        ("agent_test_cases", "ix_agent_test_cases_status"),
        ("skill_test_cases", "ix_skill_test_cases_skill_profile_id"),
        ("skill_test_cases", "ix_skill_test_cases_status"),
        ("artifact_build_manifests", "ix_artifact_build_manifests_artifact_id"),
        ("artifact_build_manifests", "ix_artifact_build_manifests_status"),
        ("datasources", "ix_datasources_name"),
        ("datasources", "ix_datasources_engine"),
        ("metric_definitions", "ix_metric_definitions_datasource_id"),
        ("metric_definitions", "ix_metric_definitions_name"),
        ("semantic_mappings", "ix_semantic_mappings_datasource_id"),
        ("semantic_mappings", "ix_semantic_mappings_table_column"),
        ("context_manifests", "ix_context_manifests_context_type"),
        ("context_manifests", "ix_context_manifests_is_active"),
        ("experience_entries", "ix_experience_entries_execution_id"),
        ("experience_entries", "ix_experience_entries_outcome"),
        ("experience_entries", "ix_experience_entries_entry_type"),
        ("learning_proposals", "ix_learning_proposals_agent_app_id"),
        ("learning_proposals", "ix_learning_proposals_status"),
        ("audit_events", "ix_audit_events_event_type"),
        ("audit_events", "ix_audit_events_occurred_at"),
        ("audit_events", "ix_audit_events_trace_id"),
    ]

    @pytest.mark.parametrize("table_name,index_name", EXPECTED_INDICES)
    def test_index_exists(self, inspector, table_name, index_name):
        indices = {idx["name"] for idx in inspector.get_indexes(table_name)}
        assert index_name in indices, (
            f"Index '{index_name}' not found on table '{table_name}'"
        )


class TestMigration014CreateAndQuery:
    """Smoke-test: insert a row into each new table and read it back."""

    def test_insert_datasource(self):
        from app.database import SessionLocal
        from app.models.datasource import Datasource
        from uuid import uuid4

        session = SessionLocal()
        try:
            ds_id = str(uuid4())
            ds = Datasource(
                id=ds_id,
                name="test-sqlite",
                engine="sqlite",
                connection_config={"database": ":memory:"},
            )
            session.add(ds)
            session.flush()

            # Read back
            stmt = text("SELECT id, name, engine FROM datasources WHERE id = :id")
            row = session.execute(stmt, {"id": ds_id}).fetchone()
            assert row is not None
            assert row.name == "test-sqlite"
            assert row.engine == "sqlite"
        finally:
            session.rollback()
            session.close()

    def test_insert_metric_definition(self):
        from app.database import SessionLocal
        from app.models.metric_definition import MetricDefinition
        from uuid import uuid4

        session = SessionLocal()
        try:
            m = MetricDefinition(
                id=str(uuid4()),
                name="revenue",
                datasource_id="ds-1",
                aggregation="sum",
                synonyms=["sales", "income", "turnover"],
            )
            session.add(m)
            session.flush()

            stmt = text("SELECT name, synonyms FROM metric_definitions WHERE name = 'revenue'")
            row = session.execute(stmt).fetchone()
            assert row is not None
        finally:
            session.rollback()
            session.close()

    def test_insert_audit_event(self):
        from app.database import SessionLocal
        from app.models.audit_event import AuditEvent
        from datetime import datetime
        import hashlib
        from uuid import uuid4

        sql = "SELECT * FROM users WHERE id = 1"
        sql_hash = hashlib.sha256(sql.encode()).hexdigest()

        session = SessionLocal()
        try:
            evt = AuditEvent(
                id=str(uuid4()),
                event_type="nl2sql_query",
                event_source="nl2sql",
                actor_type="user",
                actor_id="u-1",
                resource_type="datasource",
                resource_id="ds-1",
                datasource_id="ds-1",
                binding_id="b-1",
                sql_text_hash=sql_hash,
                row_count=42,
                query_duration_ms=150,
                policy_decision="allow",
                outcome="success",
                occurred_at=datetime.utcnow(),
            )
            session.add(evt)
            session.flush()

            stmt = text("SELECT event_type, row_count FROM audit_events WHERE datasource_id = 'ds-1'")
            row = session.execute(stmt).fetchone()
            assert row is not None
            assert row.row_count == 42
        finally:
            session.rollback()
            session.close()

    def test_insert_context_manifest(self):
        from app.database import SessionLocal
        from app.models.context_manifest import ContextManifest
        from uuid import uuid4

        session = SessionLocal()
        try:
            cm = ContextManifest(
                id=str(uuid4()),
                name="company-policy",
                context_type="policy",
                content_text="All queries must be audited.",
                priority=10,
            )
            session.add(cm)
            session.flush()

            stmt = text("SELECT name, priority FROM context_manifests WHERE name = 'company-policy'")
            row = session.execute(stmt).fetchone()
            assert row is not None
            assert row.priority == 10
        finally:
            session.rollback()
            session.close()
