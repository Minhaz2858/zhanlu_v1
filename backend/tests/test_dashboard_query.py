import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import pytest
from app.services.dashboard_query import validate_widget_sql, clamp_refresh_interval


def test_select_allowed():
    validate_widget_sql("SELECT 1")  # no raise
    validate_widget_sql("  with cte as (select 1) select * from cte")


@pytest.mark.parametrize("bad", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a=1",
    "DELETE FROM t",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN x INT",
    "CREATE TABLE t (x INT)",
    "TRUNCATE t",
    "GRANT SELECT ON t TO u",
    "REVOKE ALL ON t FROM u",
])
def test_ddl_dml_rejected(bad):
    with pytest.raises(ValueError):
        validate_widget_sql(bad)


def test_multi_statement_rejected():
    with pytest.raises(ValueError):
        validate_widget_sql("SELECT 1; DROP TABLE t")


def test_trailing_semicolon_ok():
    validate_widget_sql("SELECT 1;")  # single stmt + trailing ; is fine


def test_empty_rejected():
    with pytest.raises(ValueError):
        validate_widget_sql("   ")


def test_clamp_refresh_interval():
    assert clamp_refresh_interval(None) == 30
    assert clamp_refresh_interval(5) == 10
    assert clamp_refresh_interval(1000) == 300
    assert clamp_refresh_interval(45) == 45


# --- run_dashboard_query (Task 3) -------------------------------------------
import asyncio
from app.services.dashboard_query import run_dashboard_query


class _FakeKb:
    id = "kb-1"
    name = "sales"
    db_type = "sqlite"
    source_kind = "database"
    max_rows_per_query = 5000
    timeout_seconds = 20


class _FakeDashboard:
    id = "dash-1"
    datasource_kb_id = "kb-1"
    definition = {
        "widgets": [
            {"id": "w1", "type": "kpi", "title": "A", "sql": "SELECT 1 AS n", "options": {}},
            {"id": "w2", "type": "line", "title": "B", "sql": "BAD SQL NOT SELECT", "options": {}},
        ]
    }


class _FakeQueryService:
    def __init__(self, db):
        pass

    def execute(self, kb_id, sql, max_rows, timeout_s):
        assert sql.strip().upper().startswith("SELECT") or sql.strip().upper().startswith("WITH")
        return {"source": {"id": kb_id}, "sql": sql, "rows": [{"n": 1}],
                "row_count": 1, "truncated": False, "elapsed_ms": 2}


def test_run_dashboard_query_isolates_widget_errors(monkeypatch):
    import app.services.dashboard_query as dq
    monkeypatch.setattr(dq, "QueryService", _FakeQueryService)

    result = asyncio.new_event_loop().run_until_complete(
        run_dashboard_query(db=None, dashboard=_FakeDashboard())
    )
    assert result["dashboard_id"] == "dash-1"
    assert "refreshed_at" in result
    w1 = result["results"]["w1"]
    assert w1["error"] is None and w1["rows"] == [{"n": 1}]
    w2 = result["results"]["w2"]
    assert w2["error"] and w2["rows"] == []  # isolated error, not blank dashboard


def test_run_dashboard_query_uses_kb_controls(monkeypatch):
    captured = {}

    class _Svc(_FakeQueryService):
        def execute(self, kb_id, sql, max_rows, timeout_s):
            captured["max_rows"] = max_rows
            captured["timeout_s"] = timeout_s
            return {"rows": [], "row_count": 0, "truncated": False,
                    "elapsed_ms": 1, "sql": sql, "source": {"id": kb_id}}

    import app.services.dashboard_query as dq
    monkeypatch.setattr(dq, "QueryService", _Svc)
    asyncio.new_event_loop().run_until_complete(
        run_dashboard_query(db=None, dashboard=_FakeDashboard())
    )
    assert captured["max_rows"] <= 5000
    assert captured["timeout_s"] <= 20
