"""Integration tests for the NL2SQL pipeline — end-to-end from question to snapshot."""

import os
import sqlite3
import tempfile
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.data_snapshot import DataSnapshot
from app.models.datasource import Datasource
from app.models.agent_data_binding import AgentDataBinding
from app.models.metric_definition import MetricDefinition
from app.models.semantic_mapping import SemanticMapping


@pytest.fixture
def pipeline_db():
    """In-memory SQLAlchemy DB with all required tables."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng)
    s = Sess()
    yield s
    s.close()


@pytest.fixture
def sqlite_ds_path():
    """Create a temporary SQLite DB with test data, return the path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT)"
    )
    con.execute("INSERT INTO customers VALUES (1,'Alice','EU'),(2,'Bob','US')")
    con.commit()
    con.close()
    yield tmp.name
    os.unlink(tmp.name)


def test_ask_uses_llm_nl2sql_and_creates_snapshot(pipeline_db, sqlite_ds_path):
    """The pipeline should call the LLM-based generator (not _build_sql) and persist a DataSnapshot."""
    from app.services.nl2sql import ask

    ds = Datasource(
        id="ds-llm-1",
        name="test-ds",
        engine="sqlite",
        connection_config={"path": sqlite_ds_path, "dialect": "sqlite"},
    )
    bd = AgentDataBinding(
        id="b-llm-1",
        agent_app_id="a-llm-1",
        datasource_id="ds-llm-1",
        allowed_tables=["customers"],
        access_mode="read_only",
    )
    pipeline_db.add_all([ds, bd])
    pipeline_db.commit()

    # Stub the LLM call to return canned SQL (deterministic, offline)
    from app.services.data_snapshot import snapshot_service as ss_mod

    orig = ss_mod.DataSnapshotService.nl2sql

    canned_sql = "SELECT id, name FROM customers"

    def _stub_nl2sql(self, question, schema_description, **kw):
        return {"sql": canned_sql, "valid": True, "errors": [], "warnings": []}

    # Pass metrics that the resolver can match
    metric = MetricDefinition(
        id="m-cust", name="customers", datasource_id="ds-llm-1",
        synonyms=["clients", "accounts"],
    )
    mapping = SemanticMapping(
        id="sm-cust", datasource_id="ds-llm-1",
        table_name="customers", column_name="name",
        display_name="Customer Name",
    )
    pipeline_db.add_all([metric, mapping])
    pipeline_db.commit()

    ss_mod.DataSnapshotService.nl2sql = _stub_nl2sql
    try:
        r = ask(
            "show customers",
            binding_id="b-llm-1",
            db=pipeline_db,
            datasource_config={"path": sqlite_ds_path, "dialect": "sqlite"},
        )
        assert r.success, f"Pipeline failed: {r.error}"
        assert r.sql == canned_sql
        assert r.data is not None
        assert r.data.row_count == 2

        snap = pipeline_db.query(DataSnapshot).first()
        assert snap is not None, "Expected a DataSnapshot to be persisted"
        assert snap.sql_query == canned_sql
        assert snap.row_count == 2
        assert "Alice" in str(snap.result_data)
    finally:
        ss_mod.DataSnapshotService.nl2sql = orig


def test_ask_rejects_invalid_llm_sql(pipeline_db, sqlite_ds_path):
    """When the LLM generates invalid SQL, the pipeline returns an error."""
    from app.services.nl2sql import ask

    ds = Datasource(
        id="ds-llm-2",
        name="test-ds2",
        engine="sqlite",
        connection_config={"path": sqlite_ds_path, "dialect": "sqlite"},
    )
    bd = AgentDataBinding(
        id="b-llm-2",
        agent_app_id="a-llm-2",
        datasource_id="ds-llm-2",
        allowed_tables=["customers"],
        access_mode="read_only",
    )
    metric = MetricDefinition(
        id="m-cust2", name="customer_data", datasource_id="ds-llm-2",
        synonyms=["users", "buyers"],
    )
    pipeline_db.add_all([ds, bd, metric])
    pipeline_db.commit()

    from app.services.data_snapshot import snapshot_service as ss_mod
    orig = ss_mod.DataSnapshotService.nl2sql

    def _stub_invalid(self, question, schema_description, **kw):
        return {"sql": "INSERT INTO customers VALUES (3,'Eve','APAC')", "valid": False,
                "errors": ["Forbidden keyword 'INSERT' detected"], "warnings": []}

    ss_mod.DataSnapshotService.nl2sql = _stub_invalid
    try:
        r = ask(
            "add a customer",
            binding_id="b-llm-2",
            db=pipeline_db,
            datasource_config={"path": sqlite_ds_path, "dialect": "sqlite"},
        )
        assert not r.success
        assert "Forbidden" in r.error or "INSERT" in r.error
    finally:
        ss_mod.DataSnapshotService.nl2sql = orig


def test_ask_retries_with_error_message_then_succeeds(pipeline_db, sqlite_ds_path):
    """When first LLM call returns invalid SQL, retry with <error-msg> appended."""
    from app.services.nl2sql import ask

    ds = Datasource(
        id="ds-retry-1", name="retry-ds", engine="sqlite",
        connection_config={"path": sqlite_ds_path, "dialect": "sqlite"},
    )
    bd = AgentDataBinding(
        id="b-retry-1", agent_app_id="a-retry-1", datasource_id="ds-retry-1",
        allowed_tables=["customers"], access_mode="read_only",
    )
    metric = MetricDefinition(
        id="m-retry", name="customers", datasource_id="ds-retry-1",
        synonyms=["clients"],
    )
    pipeline_db.add_all([ds, bd, metric])
    pipeline_db.commit()

    from app.services.data_snapshot import snapshot_service as ss_mod
    orig = ss_mod.DataSnapshotService.nl2sql

    call_count = [0]

    def _stub_retry(self, question, schema_description, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"sql": "SELEC * FROM customers", "valid": False,
                    "errors": ["syntax error near 'SELEC'"], "warnings": []}
        return {"sql": "SELECT id, name FROM customers", "valid": True,
                "errors": [], "warnings": []}

    ss_mod.DataSnapshotService.nl2sql = _stub_retry
    try:
        r = ask(
            "show customers",
            binding_id="b-retry-1",
            db=pipeline_db,
            datasource_config={"path": sqlite_ds_path, "dialect": "sqlite"},
        )
        assert r.success, f"Pipeline should succeed after retry, got: {r.error}"
        assert call_count[0] == 2, f"Expected 2 calls, got {call_count[0]}"
        assert r.sql == "SELECT id, name FROM customers"
    finally:
        ss_mod.DataSnapshotService.nl2sql = orig


def test_ask_passes_dialect_rules_in_schema_description(pipeline_db, sqlite_ds_path):
    """The schema_description passed to the LLM should include dialect quoting rules."""
    from app.services.nl2sql import ask

    ds = Datasource(
        id="ds-dialect-1", name="dialect-ds", engine="sqlite",
        connection_config={"path": sqlite_ds_path, "dialect": "sqlite"},
    )
    bd = AgentDataBinding(
        id="b-dialect-1", agent_app_id="a-dialect-1", datasource_id="ds-dialect-1",
        allowed_tables=["customers"], access_mode="read_only",
    )
    metric = MetricDefinition(
        id="m-dialect", name="customers", datasource_id="ds-dialect-1",
        synonyms=["clients"],
    )
    pipeline_db.add_all([ds, bd, metric])
    pipeline_db.commit()

    from app.services.data_snapshot import snapshot_service as ss_mod
    orig = ss_mod.DataSnapshotService.nl2sql

    captured_schema_desc = []

    def _stub_capture(self, question, schema_description, **kw):
        captured_schema_desc.append(schema_description)
        return {"sql": "SELECT id, name FROM customers", "valid": True, "errors": [], "warnings": []}

    ss_mod.DataSnapshotService.nl2sql = _stub_capture
    try:
        r = ask(
            "show customers",
            binding_id="b-dialect-1",
            db=pipeline_db,
            datasource_config={"path": sqlite_ds_path, "dialect": "sqlite"},
        )
        assert r.success
        assert captured_schema_desc, "schema_description should have been captured"
        desc = captured_schema_desc[0]
        assert "<dialect-rules>" in desc, f"Expected dialect rules, got: {desc[:200]}"
        assert "double-quotes" in desc or "SQLite" in desc
    finally:
        ss_mod.DataSnapshotService.nl2sql = orig
