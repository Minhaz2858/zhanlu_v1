"""Regression test: engine._fetch_series must produce MySQL-safe SQL.

The engine used hardcoded double-quote identifiers (Postgres-style), which
break on MySQL. The fix is to use per-db_type identifier quoting. This test
catches the bug by snapshotting the generated SQL for a known target.
"""

import pytest


def _make_target(name: str, table: str, time_col: str, measure: str, dims: list[str] | None = None) -> object:
    """Build a minimal object shaped like a ForecastTarget for the engine."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=name, name=name, org_id="org-1", app_id="app-1",
        product_key=name, is_deleted=False,
        datasource={"table": table, "time_column": time_col, "measure": measure, "dimensions": dims or []},
    )


def test_fetch_series_sql_uses_mysql_backticks_for_mysql_kb(monkeypatch):
    """For a MySQL KB, the generated SQL must use backticks (not double-quotes)
    so MySQL accepts the query."""
    from unittest.mock import MagicMock
    from app.services.forecasting.engine import ForecastEngine

    fake_db = MagicMock()
    # KB lookup returns a MySQL KB
    fake_kb = MagicMock()
    fake_kb.db_type = "mysql"
    fake_db.query.return_value.filter.return_value.first.return_value = fake_kb

    # QueryService.execute returns one row so we can inspect the SQL it received
    captured_sql: list[str] = []

    class _FakeQuerySvc:
        def __init__(self, *_a, **_kw): pass
        def execute(self, kb_id, sql, **_kw):
            captured_sql.append(sql)
            return {"rows": [{"t": "2025-01-01", "y": 100.0}], "row_count": 1}

    monkeypatch.setattr(
        "app.services.forecasting.engine.QueryService", _FakeQuerySvc
    )

    fe = ForecastEngine(fake_db)
    target = _make_target(
        "tgt-1", table="lz_v_裂解c5_data",
        time_col="biz_date(业务日期)", measure="tax_price(含税单价)",
        dims=["material_name(产品名称)"],
    )
    fe._fetch_series(target)

    assert captured_sql, "QueryService.execute was not called"
    sql = captured_sql[0]
    # Every identifier must be backtick-quoted
    assert "`" in sql, f"SQL has no backticks (MySQL needs them): {sql}"
    # No double-quote identifiers (those are for Postgres)
    import re
    bad = re.findall(r'"[a-zA-Z_()\u4e00-\u9fff]+"', sql)
    # ALLOW double-quoted string literals, but not double-quoted identifiers
    # An identifier is a double-quoted name that has no spaces and is referenced as a column.
    # Simpler check: any double-quoted text that doesn't contain SQL string-escapes is suspect.
    assert len(bad) == 0, f"SQL has Postgres-style double-quoted identifiers: {bad}\nFull SQL: {sql}"


def test_fetch_series_sql_uses_double_quotes_for_postgres_kb(monkeypatch):
    """For a Postgres KB, the generated SQL must use double quotes (not backticks)
    so Postgres accepts the query."""
    from unittest.mock import MagicMock
    from app.services.forecasting.engine import ForecastEngine

    fake_db = MagicMock()
    fake_kb = MagicMock()
    fake_kb.db_type = "postgres"
    fake_db.query.return_value.filter.return_value.first.return_value = fake_kb

    captured_sql: list[str] = []

    class _FakeQuerySvc:
        def __init__(self, *_a, **_kw): pass
        def execute(self, kb_id, sql, **_kw):
            captured_sql.append(sql)
            return {"rows": [{"t": "2025-01-01", "y": 100.0}], "row_count": 1}

    monkeypatch.setattr(
        "app.services.forecasting.engine.QueryService", _FakeQuerySvc
    )

    fe = ForecastEngine(fake_db)
    target = _make_target(
        "tgt-1", table="my_table",
        time_col="date", measure="price", dims=[],
    )
    fe._fetch_series(target)

    assert captured_sql
    sql = captured_sql[0]
    assert '"' in sql, f"SQL has no double-quotes (Postgres needs them): {sql}"
    assert "`" not in sql, f"SQL has backticks (Postgres doesn't allow them): {sql}"
