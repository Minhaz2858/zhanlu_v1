"""Connection-fingerprint schema caching — root fix for wrong-table-name
after a user points a KB at a DIFFERENT database.

The schema caches (db_tools._SCHEMA_CACHE and SchemaService._SCHEMA_CACHE)
were keyed by kb_id ONLY. Reconnecting the same KB to another host/database
returned the OLD database's tables for up to the TTL (1h), so agents wrote
SQL with wrong table names. Fix: include a stable fingerprint of the
connection identity (db_type/host/port/database_name/schema) in every cache
key — a different connection is automatically a cache miss.

Rules verified here:
* fingerprint changes when connection identity changes, stable when not
* fail-soft: missing KB / unknown fields -> "" (key degrades to kb_id-only,
  never raises, never blocks execution)
* db_tools cache key differs when fingerprint differs
* SchemaService.list_tables re-queries the connector when the KB's
  connection identity changes (same kb_id!)
"""
from __future__ import annotations

import time

import pytest

from app.services.db.schema_service import (
    SchemaService,
    connection_fingerprint,
    invalidate_schema_cache,
)
from app.services.tool_handlers.db_tools import _schema_cache_key

# ---------------------------------------------------------------------------
# Fixtures: a fake db session that returns a configurable fake KB row
# ---------------------------------------------------------------------------


class FakeKB:
    def __init__(self, **kw):
        self.id = kw.get("id", "kb-1")
        self.name = kw.get("name", "Fake KB")
        self.db_type = kw.get("db_type", "mysql")
        self.host = kw.get("host", "10.0.0.1")
        self.port = kw.get("port", 3306)
        self.database_name = kw.get("database_name", "erp_prod")
        self.schema = kw.get("schema", None)
        self.username = kw.get("username", "reader")
        self.source_kind = kw.get("source_kind", "db")


class FakeDB:
    def __init__(self, kb):
        self._kb = kb
        self.calls = 0

    def get(self, model, kb_id):
        self.calls += 1
        if self._kb is not None and self._kb.id == kb_id:
            return self._kb
        return None

    def set_kb(self, kb):
        """Simulate the user editing the datasource connection."""
        self._kb = kb


# ---------------------------------------------------------------------------
# connection_fingerprint behavior
# ---------------------------------------------------------------------------


def test_fingerprint_stable_for_same_connection():
    db = FakeDB(FakeKB())
    a = connection_fingerprint(db, "kb-1")
    b = connection_fingerprint(db, "kb-1")
    assert a == b
    assert a != ""


def test_fingerprint_changes_when_host_changes():
    db1 = FakeDB(FakeKB(host="10.0.0.1"))
    db2 = FakeDB(FakeKB(host="10.0.0.2"))
    assert connection_fingerprint(db1, "kb-1") != connection_fingerprint(db2, "kb-1")


def test_fingerprint_changes_when_database_name_changes():
    db1 = FakeDB(FakeKB(database_name="erp_prod"))
    db2 = FakeDB(FakeKB(database_name="erp_test"))
    assert connection_fingerprint(db1, "kb-1") != connection_fingerprint(db2, "kb-1")


def test_fingerprint_changes_when_dialect_changes():
    db1 = FakeDB(FakeKB(db_type="mysql"))
    db2 = FakeDB(FakeKB(db_type="postgres"))
    assert connection_fingerprint(db1, "kb-1") != connection_fingerprint(db2, "kb-1")


def test_fingerprint_fail_soft_on_missing_kb():
    db = FakeDB(None)
    assert connection_fingerprint(db, "kb-nope") == ""


def test_fingerprint_fail_soft_on_exception():
    class BoomDB:
        def get(self, model, kb_id):
            raise RuntimeError("db down")

    assert connection_fingerprint(BoomDB(), "kb-1") == ""


# ---------------------------------------------------------------------------
# db_tools cache key
# ---------------------------------------------------------------------------


def test_db_tools_cache_key_includes_fingerprint():
    k1 = _schema_cache_key("kb-1", None, True, 50, fingerprint="fp-a")
    k2 = _schema_cache_key("kb-1", None, True, 50, fingerprint="fp-b")
    assert k1 != k2
    assert "fp-a" in k1


def test_db_tools_cache_key_backward_compatible_without_fingerprint():
    k = _schema_cache_key("kb-1", "t1", False, 50)
    assert k.startswith("kb-1|")
    assert "t1" in k


# ---------------------------------------------------------------------------
# SchemaService.list_tables cache must miss when connection identity changes
# ---------------------------------------------------------------------------


class FakeConnector:
    def __init__(self, tables):
        self._tables = tables
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_tables(self):
        self.calls += 1
        return list(self._tables)


def test_list_tables_requeries_after_connection_change(monkeypatch):
    kb1 = FakeKB(host="10.0.0.1", database_name="erp_prod")
    kb2 = FakeKB(host="10.0.0.2", database_name="erp_prod")  # SAME kb id, new host
    db = FakeDB(kb1)
    svc = SchemaService(db)  # type: ignore[arg-type]

    conn = FakeConnector(["table_a"])

    def fake_load(kb_id):
        return db._kb

    def fake_get_connector(kb):
        return conn

    monkeypatch.setattr(svc, "_load_kb", fake_load)
    monkeypatch.setattr(
        "app.services.db.schema_service.get_connector", fake_get_connector
    )

    invalidate_schema_cache()
    first = svc.list_tables("kb-1")
    assert first["tables"] == ["table_a"]
    assert conn.calls == 1

    # Same connection -> cache hit, no new connector call.
    svc.list_tables("kb-1")
    assert conn.calls == 1

    # Connection identity changes (same kb_id!) -> cache MISS -> re-query.
    db.set_kb(kb2)  # user edited the datasource: new host, same KB id
    conn2 = FakeConnector(["table_b"])
    monkeypatch.setattr(
        "app.services.db.schema_service.get_connector", lambda kb: conn2
    )
    second = svc.list_tables("kb-1")
    assert second["tables"] == ["table_b"]
    assert conn2.calls == 1


class FakeDescribeConnector(FakeConnector):
    def __init__(self, columns_by_table):
        super().__init__(list(columns_by_table.keys()))
        self._cols = columns_by_table

    def describe_table(self, table):
        self.calls += 1
        return [{"name": c, "data_type": "varchar"} for c in self._cols.get(table, [])]


def test_describe_table_requeries_after_connection_change(monkeypatch):
    """The schema validator reads via describe_table — it must see the NEW
    connection's columns after a reconnect, or a wrong-table-name SQL that
    WAS valid against the old DB would keep passing validation."""
    kb1 = FakeKB(host="10.0.0.1", database_name="erp_prod")
    kb2 = FakeKB(host="10.0.0.1", database_name="erp_test")  # SAME host, new DB
    db = FakeDB(kb1)
    svc = SchemaService(db)  # type: ignore[arg-type]

    def fake_load(kb_id):
        return db._kb

    monkeypatch.setattr(svc, "_load_kb", fake_load)

    conn = FakeDescribeConnector({"sales": ["revenue", "qty"]})
    monkeypatch.setattr(
        "app.services.db.schema_service.get_connector", lambda kb: conn
    )
    invalidate_schema_cache()

    first = svc.describe_table("kb-1", "sales")
    assert [c["name"] for c in first["columns"]] == ["revenue", "qty"]
    assert conn.calls == 1

    # Same connection -> cache hit.
    svc.describe_table("kb-1", "sales")
    assert conn.calls == 1

    # User points the KB at a different database (same host) -> re-query.
    db.set_kb(kb2)
    conn2 = FakeDescribeConnector({"sales": ["amount", "region"]})
    monkeypatch.setattr(
        "app.services.db.schema_service.get_connector", lambda kb: conn2
    )
    second = svc.describe_table("kb-1", "sales")
    assert [c["name"] for c in second["columns"]] == ["amount", "region"]
    assert conn2.calls == 1


class FakeManyConnector(FakeConnector):
    """Connector with N tables; describe_table returns one column."""

    def __init__(self, n):
        super().__init__([f"tbl_{i:03d}" for i in range(n)])
        self.describe_calls = 0

    def describe_table(self, table):
        self.describe_calls += 1
        return [{"name": "col_a", "data_type": "int"}]


def test_describe_all_exposes_full_table_list_beyond_cap(monkeypatch):
    """When a DB has more tables than max_tables, the agent must still learn
    every table NAME (cheap) so it can describe_table any of them — otherwise
    alphabetical truncation hides business views and the agent guesses."""
    kb = FakeKB(host="10.0.0.1", database_name="big_db")
    db = FakeDB(kb)
    svc = SchemaService(db)  # type: ignore[arg-type]

    def fake_load(kb_id):
        return kb

    monkeypatch.setattr(svc, "_load_kb", fake_load)
    conn = FakeManyConnector(120)
    monkeypatch.setattr(
        "app.services.db.schema_service.get_connector", lambda kb: conn
    )
    invalidate_schema_cache()

    result = svc.describe_all("kb-1", max_tables=50)
    # Only 50 detailed entries (bounded work), but ALL 120 names exposed.
    assert len(result["tables"]) == 50
    assert result["truncated"] is True
    assert len(result["all_table_names"]) == 120
    assert result["all_table_names"][0] == "tbl_000"
    assert result["all_table_names"][-1] == "tbl_119"
    # The connector only described 50 — the name list is free.
    assert conn.describe_calls == 50
