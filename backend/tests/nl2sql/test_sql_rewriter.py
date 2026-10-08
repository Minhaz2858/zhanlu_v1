"""SQL Rewriter — the OCI stage-5 optimization pass (deterministic, safe).

Pins the contract: every rule is semantics-preserving or it does not rewrite.
A rewrite that could change the answer (row count, grain, filter) must be
recorded as a note, never applied. Nothing here may break the invariants the
execution gate enforces (row filters, read-only, table/column existence).
"""

from __future__ import annotations

import pytest

from app.services.db.sql_rewriter import rewrite_sql


# ── passthrough ────────────────────────────────────────────────────────────


def test_non_select_is_never_rewritten():
    sql = "UPDATE t SET x = 1 WHERE id = 2"
    r = rewrite_sql(sql)
    assert r.sql == sql
    assert r.applied == []


def test_clean_sql_is_unchanged():
    sql = "SELECT id, name FROM orders WHERE status = 'open' ORDER BY id LIMIT 20"
    r = rewrite_sql(sql)
    assert r.sql == sql
    assert r.applied == []


def test_garbage_never_raises():
    r = rewrite_sql("SELEC * FORM")
    assert r.applied == []
    assert r.sql == "SELEC * FORM"


# ── limit enforcement (exploratory path only) ─────────────────────────────


def test_limit_added_when_none_and_max_rows_given():
    r = rewrite_sql("SELECT id FROM big_table WHERE x = 1", max_rows=50)
    assert r.sql.rstrip().rstrip(";").lower().endswith("limit 50")
    assert "limit_enforcement" in r.applied


def test_limit_respects_order_by():
    r = rewrite_sql("SELECT id FROM big_table ORDER BY id DESC", max_rows=10)
    low = r.sql.lower()
    assert low.index("order by") < low.index("limit 10")
    assert "limit_enforcement" in r.applied


def test_no_limit_when_one_exists():
    sql = "SELECT id FROM big_table LIMIT 5"
    r = rewrite_sql(sql, max_rows=50)
    assert r.sql == sql
    assert "limit_enforcement" not in r.applied


def test_no_limit_for_mssql_top():
    sql = "SELECT TOP (10) id FROM big_table"
    r = rewrite_sql(sql, max_rows=50)
    assert r.sql == sql
    assert "limit_enforcement" not in r.applied


def test_no_limit_for_fetch_first():
    sql = "SELECT id FROM big_table OFFSET 0 ROWS FETCH FIRST 10 ROWS ONLY"
    r = rewrite_sql(sql, max_rows=50)
    assert "limit_enforcement" not in r.applied


def test_no_limit_when_max_rows_missing():
    sql = "SELECT id FROM big_table"
    r = rewrite_sql(sql)
    assert r.sql == sql
    assert "limit_enforcement" not in r.applied


def test_limit_on_union_applies_to_the_union():
    sql = "SELECT id FROM a UNION SELECT id FROM b"
    r = rewrite_sql(sql, max_rows=5)
    assert r.sql.lower().endswith("limit 5")
    assert "limit_enforcement" in r.applied


# ── predicate push-down (conservative) ────────────────────────────────────


def test_predicate_pushed_into_plain_derived_table():
    sql = (
        "SELECT t.a, t.b FROM ("
        "SELECT a, b, c FROM base WHERE d = 1"
        ") t WHERE t.b = 'x'"
    )
    r = rewrite_sql(sql)
    assert "predicate_pushdown" in r.applied
    # The inner query now carries the equality; the outer keeps it too
    # (belt and braces: pushing is an optimization, not a filter move).
    inner = r.sql[r.sql.index("(") : r.sql.rindex(")")]
    assert "b = 'x'" in inner


def test_pushdown_skipped_when_inner_aggregates():
    sql = (
        "SELECT t.a, t.n FROM ("
        "SELECT a, COUNT(*) AS n FROM base GROUP BY a"
        ") t WHERE t.a = 'x'"
    )
    r = rewrite_sql(sql)
    assert "predicate_pushdown" not in r.applied
    inner = r.sql[r.sql.index("(") : r.sql.rindex(")")]
    assert "= 'x'" not in inner


def test_pushdown_skipped_when_inner_has_distinct_or_limit():
    for modifier in ("SELECT DISTINCT a, b FROM base", "SELECT a, b FROM base LIMIT 3"):
        sql = f"SELECT t.a FROM ({modifier}) t WHERE t.a = 1"
        r = rewrite_sql(sql)
        assert "predicate_pushdown" not in r.applied


def test_pushdown_skipped_for_non_literal_predicates():
    sql = (
        "SELECT t.a FROM (SELECT a, b FROM base) t "
        "WHERE t.a = t.b"
    )
    r = rewrite_sql(sql)
    assert "predicate_pushdown" not in r.applied


# ── join elimination (only with uniqueness evidence) ──────────────────────


def test_join_eliminated_only_with_unique_evidence():
    sql = (
        "SELECT c.id, c.name FROM customers c "
        "INNER JOIN region_dict r ON r.code = c.region_code"
    )
    # No evidence → NOT removed (a join can fan out or filter rows).
    r = rewrite_sql(sql)
    assert "join_elimination" not in r.applied
    assert "region_dict" in r.sql

    # The joined side's key is unique → removal is row-count-safe.
    r2 = rewrite_sql(sql, unique_columns={"region_dict": {"code"}})
    assert "join_elimination" in r2.applied
    assert "region_dict" not in r2.sql
    assert "customers" in r2.sql


def test_referenced_join_is_never_removed():
    sql = (
        "SELECT c.id, r.label FROM customers c "
        "INNER JOIN region_dict r ON r.code = c.region_code"
    )
    r = rewrite_sql(sql, unique_columns={"region_dict": {"code"}})
    assert "join_elimination" not in r.applied
    assert "region_dict" in r.sql


def test_join_in_where_predicate_is_never_removed():
    sql = (
        "SELECT c.id FROM customers c "
        "INNER JOIN region_dict r ON r.code = c.region_code "
        "WHERE r.label = 'north'"
    )
    r = rewrite_sql(sql, unique_columns={"region_dict": {"code"}})
    assert "join_elimination" not in r.applied


# ── telemetry-only notes (never silent rewrites) ──────────────────────────


def test_count_col_is_a_note_not_a_rewrite():
    sql = "SELECT COUNT(status) FROM orders"
    r = rewrite_sql(sql)
    assert r.sql == sql
    assert any("count" in n.lower() for n in r.notes)


def test_select_star_is_a_note_not_a_rewrite():
    sql = "SELECT * FROM orders"
    r = rewrite_sql(sql)
    assert r.sql == sql
    assert any("select_star" in n.lower() for n in r.notes)


# ── the flag-gated stage helper in the canonical pipeline ─────────────────


def test_stage_helper_off_returns_none(monkeypatch):
    from app.config import settings
    from app.services.db import nl_answer_service as nls

    monkeypatch.setattr(settings, "NL2SQL_SQL_REWRITE_ENABLED", False)
    assert nls._apply_sql_rewrite_stage("SELECT id FROM big_table", max_rows=5) is None


def test_stage_helper_on_rewrites(monkeypatch):
    from app.config import settings
    from app.services.db import nl_answer_service as nls

    monkeypatch.setattr(settings, "NL2SQL_SQL_REWRITE_ENABLED", True)
    r = nls._apply_sql_rewrite_stage("SELECT id FROM big_table", max_rows=5)
    assert r is not None
    assert "limit_enforcement" in r.applied
