import sqlite3

import pytest

from app.services.dashboard_profiler import profile_engine


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "profile.db"
    con = sqlite3.connect(p)
    con.executescript(
        """
        CREATE TABLE sales_orders (
            product TEXT,
            amount REAL,
            created_at TEXT,
            region TEXT
        );
        INSERT INTO sales_orders VALUES
            ('C5 Resin', 120.5, '2026-01-05', 'East'),
            ('C5 Resin', 99.0, '2026-02-10', 'East'),
            ('Isoprene', 250.0, '2026-03-15', 'West'),
            ('C5 Resin', NULL, '2026-04-20', NULL),
            ('C9 Resin', 88.0, '2026-05-25', 'East');
        """
    )
    con.commit()
    con.close()
    return str(p)


def test_profile_engine_returns_expected_shape(db_path):
    result = profile_engine(db_path, "sales_orders", ["product", "amount", "created_at", "region"])
    assert result["table"] == "sales_orders"
    assert result["row_count"] == 5
    assert result["status"] == "ok"
    cols = {c["name"]: c for c in result["columns"]}
    # Fixture has 3 distinct products: C5 Resin (3 rows), Isoprene, C9 Resin.
    # (Plan draft said 4 — corrected to match the actual fixture data.)
    assert cols["product"]["cardinality"] == 3
    assert cols["product"]["null_pct"] == 0.0
    assert cols["product"]["shape"] == "category"
    assert cols["amount"]["null_pct"] == pytest.approx(0.2)
    assert cols["created_at"]["shape"] == "time_series"
    assert len(cols["region"]["top_values"]) >= 2


def test_profile_engine_missing_table_reports_error(db_path):
    result = profile_engine(db_path, "no_such_table", ["a"])
    assert result["status"] == "error"
    assert result["error_message"]


def test_profile_engine_unknown_column_reports_error(db_path):
    result = profile_engine(db_path, "sales_orders", ["nope"])
    assert result["status"] == "error"
    assert result["error_message"]


def test_profile_uses_dialect_quote_ident(monkeypatch):
    import app.services.dashboard_profiler as mod

    seen = {}

    class FakeConn:
        def __init__(self, dialect):
            self.dialect = dialect

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, max_rows=5, timeout_s=10):
            seen["sql"] = sql
            if sql.strip().upper().startswith("SELECT COUNT(*)"):
                return [{"row_count": 3}]
            if "DISTINCT" in sql.upper():
                return [{"cardinality": 2, "non_null": 3, "min_value": "a", "max_value": "c"}]
            return [{"sample_value": "a"}, {"sample_value": "b"}]

    class FakeSchema:
        def __init__(self, db):
            pass

        def describe_table(self, kb_id, table):
            return {"columns": [{"name": "col_a", "type": "varchar"}]}

    mod.get_connector = lambda kb: FakeConn("mysql")
    mod._load_kb = lambda db, kb_id: type("KB", (), {"db_type": "mysql"})()
    mod._infer_col_type = lambda db, kb_id, table, col, samples: "text"
    mod.SchemaService = FakeSchema

    result = mod.profile_kb(None, "kb_1", "tbl", ["col_a"])
    assert "`tbl`" in seen["sql"] and "`col_a`" in seen["sql"]
    assert result["status"] == "ok"
    assert result["row_count"] == 3


def test_profile_engine_max_columns_cap(db_path):
    result = profile_engine(
        db_path, "sales_orders",
        ["product", "amount", "created_at", "region"],
        max_columns=2,
    )
    assert result["status"] == "ok"
    assert [c["name"] for c in result["columns"]] == ["product", "amount"]


def test_profile_engine_top_values_exclude_none(db_path):
    result = profile_engine(db_path, "sales_orders", ["region"])
    assert result["status"] == "ok"
    assert None not in result["columns"][0]["top_values"]


def test_profile_kb_unknown_column_reports_error(monkeypatch):
    import app.services.dashboard_profiler as mod

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, max_rows=5, timeout_s=10):
            if sql.strip().upper().startswith("SELECT COUNT(*)"):
                return [{"row_count": 3}]
            # Garbage — must never be reached for the unknown column.
            return [
                {"cardinality": 1, "non_null": 3, "min_value": "nope", "max_value": "nope"}
            ]

    class FakeSchema:
        def __init__(self, db):
            pass

        def describe_table(self, kb_id, table):
            return {"columns": [{"name": "real_col", "type": "varchar"}]}

    mod.get_connector = lambda kb: FakeConn()
    mod._load_kb = lambda db, kb_id: type("KB", (), {"db_type": "sqlite"})()
    mod.SchemaService = FakeSchema

    result = mod.profile_kb(None, "kb_1", "tbl", ["nope"])
    assert result["status"] == "error"
    assert "nope" in result["error_message"]
    assert result["columns"] == []


def test_profile_kb_empty_table_reports_empty(monkeypatch):
    import app.services.dashboard_profiler as mod

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, max_rows=5, timeout_s=10):
            return [{"row_count": 0}]

    class FakeSchema:
        def __init__(self, db):
            pass

        def describe_table(self, kb_id, table):
            return {"columns": [{"name": "col_a", "type": "varchar"}]}

    mod.get_connector = lambda kb: FakeConn()
    mod._load_kb = lambda db, kb_id: type("KB", (), {"db_type": "sqlite"})()
    mod._infer_col_type = lambda db, kb_id, table, col, samples: "text"
    mod.SchemaService = FakeSchema

    result = mod.profile_kb(None, "kb_1", "tbl", ["col_a"])
    assert result["status"] == "empty"
    assert result["row_count"] == 0
