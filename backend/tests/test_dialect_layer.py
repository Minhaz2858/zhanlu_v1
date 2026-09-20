"""P2 — the dialect capability layer.

Pins one contract per dialect: identifier quoting, row capping, date/timestamp
expressions, null + string functions, probes, EXPLAIN. These are the statements
that actually reach a customer database, so a regression here is a syntax error
on a live source.

The behavioural reason this suite exists is the state it replaced:

* ``nl2sql/dialect_rules.quote_rule`` had no Oracle or SQL Server entry and
  returned the POSTGRESQL text for them (``_RULES.get(key, _RULES["postgres"])``)
  — an Oracle source was prompted to quote like PostgreSQL;
* ``LIMIT`` was hardcoded in ``db/schema_graph``, ``catalog_indexer`` and
  ``nl_answer_service`` — a syntax error on SQL Server and Oracle;
* ``schema_graph._quote_ident`` emitted double quotes for everything that was
  not MySQL — wrong brackets for SQL Server;
* ``_probe_table_coverage`` chose ``(kb.db_type or "mysql")`` — a silent
  default dialect.
"""

from __future__ import annotations

import re

import pytest

from app.services.db.dialect import (
    UNKNOWN_DIALECT_HINT,
    UnknownDialect,
    known_dialects,
    maybe_profile,
    normalize_dialect,
    profile_for,
)


# ── dialect resolution ─────────────────────────────────────────────────────


def test_known_dialects_cover_every_shipped_connector():
    """Every connector the factory can build must have a capability profile."""
    from app.services.db.connector_factory import supported_db_types

    unimplemented = sorted(
        dt for dt in supported_db_types() if maybe_profile(dt) is None
    )
    assert not unimplemented, (
        f"connectors advertised without dialect rules: {unimplemented} — an "
        "advertised dialect with no profile is one that cannot be executed against"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mysql", "mysql"),
        ("MySQL", "mysql"),
        ("mariadb", "mysql"),
        ("postgres", "postgres"),
        ("postgresql", "postgres"),
        ("PostgreSQL", "postgres"),
        ("postgresql+psycopg2", "postgres"),
        ("sqlite", "sqlite"),
        ("sqlite3", "sqlite"),
        ("mssql", "mssql"),
        ("sqlserver", "mssql"),
        ("SQL Server", "mssql"),
        ("sql-server", "mssql"),
        ("oracle", "oracle"),
        ("Oracle", "oracle"),
        ("oracle+cx_oracle", "oracle"),
    ],
)
def test_dialect_aliases_resolve(raw, expected):
    assert profile_for(raw).name == expected


@pytest.mark.parametrize("raw", ["db2", "snowflake", "", None, "   ", "unknown"])
def test_unknown_dialect_raises_instead_of_defaulting(raw):
    """No default dialect: an unidentified source raises, it does not borrow.

    This is the rule the old code broke in three places at once (PostgreSQL
    quoting for unknown dialects, ``or "mysql"`` for the coverage probe,
    ``"default"`` in the dashboard builder).
    """
    assert maybe_profile(raw) is None
    with pytest.raises(UnknownDialect):
        profile_for(raw)
    assert set(known_dialects()) == {"mysql", "postgres", "sqlite", "mssql", "oracle"}


def test_normalize_keeps_casing_and_separators_out():
    assert normalize_dialect("  SQL-Server ") == "sqlserver"


# ── MySQL ──────────────────────────────────────────────────────────────────


def test_mysql_quoting_and_dates():
    p = profile_for("mysql")
    assert p.quote_ident("order") == "`order`"
    assert p.qualified("sales.order") == "`sales`.`order`"
    assert p.month_of("FDATE") == "DATE_FORMAT(`FDATE`, '%Y-%m')"
    assert p.day_of("FDATE") == "DATE(`FDATE`)"
    assert p.current_date() == "CURDATE()"
    assert p.add_days("FDATE", 7) == "DATE_ADD(`FDATE`, INTERVAL 7 DAY)"
    assert p.days_between("a", "b") == "DATEDIFF(`a`, `b`)"
    assert p.null_coalesce("x", "0") == "IFNULL(x, 0)"


def test_mysql_row_cap_and_probes():
    p = profile_for("mysql")
    assert p.paged("SELECT 1 FROM t", 10).endswith("LIMIT 10")
    assert p.sample_sql("customers", "id,name", 5) == (
        "SELECT `id`, `name` FROM `customers` LIMIT 5"
    )
    assert "information_schema.TABLES" in p.row_count_sql_for("customers")
    assert p.explain("SELECT 1") == "EXPLAIN SELECT 1"


# ── PostgreSQL ─────────────────────────────────────────────────────────────


def test_postgres_quoting_and_dates():
    p = profile_for("postgres")
    assert p.quote_ident("order") == '"order"'
    assert p.month_of("created_at") == "TO_CHAR(\"created_at\", 'YYYY-MM')"
    assert p.day_of("created_at") == '("created_at")::date'
    assert p.current_date() == "CURRENT_DATE"
    assert p.add_days("created_at", 3) == "(\"created_at\" + INTERVAL '3 days')"
    assert p.days_between("a", "b") == '(("a")::date - ("b")::date)'
    assert p.null_coalesce("x", "0") == "COALESCE(x, 0)"
    assert p.concat("a", "b") == "(a || b)"


def test_postgres_row_cap_and_probes():
    p = profile_for("postgres")
    assert p.paged("SELECT 1 FROM t", 10).endswith("LIMIT 10")
    assert "pg_class" in p.row_count_sql_for("customers")
    assert p.explain("SELECT 1") == "EXPLAIN SELECT 1"


# ── SQLite ─────────────────────────────────────────────────────────────────


def test_sqlite_behavior():
    p = profile_for("sqlite")
    assert p.quote_ident("order") == '"order"'
    assert p.paged("SELECT 1 FROM t", 10).endswith("LIMIT 10")
    assert p.month_of("d") == "STRFTIME('%Y-%m', \"d\")"
    assert p.current_date() == "DATE('now')"
    # No statistics table — returns None instead of faking a COUNT(*) scan.
    assert p.row_count_sql_for("customers") is None
    assert p.explain("SELECT 1") == "EXPLAIN SELECT 1"
    # SQLite stores no timezone/locale: the probe set is empty, not guessed.
    assert p.timezone_sql is None
    assert p.locale_sql is None


# ── Oracle ─────────────────────────────────────────────────────────────────


def test_oracle_capability_mapping():
    p = profile_for("oracle")
    assert p.quote_ident("ORDER") == '"ORDER"'
    # No LIMIT, no TOP — 12c+ FETCH FIRST. This is the statement that used to be
    # "SELECT 1 FROM t LIMIT 1" and fail.
    assert p.paged("SELECT 1 FROM t", 1) == "SELECT 1 FROM t FETCH FIRST 1 ROWS ONLY"
    assert p.month_of("FDATE") == "TO_CHAR(\"FDATE\", 'YYYY-MM')"
    assert p.day_of("FDATE") == 'TRUNC("FDATE")'
    assert p.current_date() == "TRUNC(SYSDATE)"
    assert p.null_coalesce("x", "0") == "NVL(x, 0)"
    assert "user_tables" in p.row_count_sql_for("CUSTOMERS")
    # EXPLAIN PLAN FOR writes to PLAN_TABLE — not an inline prefix.
    assert p.explain("SELECT 1") is None
    assert not p.explain_prefix
    hint = p.prompt_hint()
    # The row-cap example must be Oracle's, not another vendor's.
    assert "FETCH FIRST 10 ROWS ONLY" in hint
    assert "LIMIT 10" not in hint.upper()


# ── SQL Server ─────────────────────────────────────────────────────────────


def test_sql_server_capability_mapping():
    p = profile_for("mssql")
    assert p.quote_ident("order") == "[order]"
    # TOP goes after SELECT (and after DISTINCT) — not a trailing LIMIT.
    assert p.paged("SELECT 1 FROM t", 10) == "SELECT TOP 10 1 FROM t"
    assert p.paged("SELECT DISTINCT x FROM t", 3) == "SELECT DISTINCT TOP 3 x FROM t"
    assert p.month_of("d") == "FORMAT([d], 'yyyy-MM')"
    assert p.current_date() == "CAST(GETDATE() AS date)"
    assert p.days_between("a", "b") == "DATEDIFF(day, [b], [a])"
    assert p.null_coalesce("x", "0") == "ISNULL(x, 0)"
    assert "sys.partitions" in p.row_count_sql_for("customers")
    assert p.explain("SELECT 1") is None
    assert "TOP" in p.prompt_hint()
    # The prompt must forbid the MySQL habit explicitly.
    assert any("LIMIT" in rule for rule in p.authoring_rules)


def test_top_only_inserted_on_statements_that_can_take_it():
    """A non-SELECT must not be silently reported as capped."""
    p = profile_for("mssql")
    stmt = "EXEC sp_who"
    assert p.paged(stmt, 5) == stmt  # unchanged, caller can see no cap was applied


# ── cross-cutting ──────────────────────────────────────────────────────────


def test_pagination_is_never_appended_for_zero_rows():
    for name in known_dialects():
        sql = "SELECT 1 FROM t"
        assert profile_for(name).paged(sql, 0) == sql


def test_quote_ident_rejects_unsafe_names_on_every_dialect():
    for name in known_dialects():
        with pytest.raises(ValueError):
            profile_for(name).quote_ident("t; DROP TABLE x")


def test_capability_dict_is_data_only():
    for name in known_dialects():
        caps = profile_for(name).capability_dict()
        assert caps["dialect"] == name
        assert caps["identifier_quoting"]["open"]
        assert caps["date_functions"]["month_bucket"]
        assert isinstance(caps["supports_window_functions"], bool)
        assert "probes" in caps


def test_prompt_hint_states_the_dialect_it_describes():
    """The prompt and the executor must agree about which dialect is targeted."""
    for name in known_dialects():
        hint = profile_for(name).prompt_hint()
        assert f"Target dialect: {name}" in hint, f"{name} hint does not name its dialect"


@pytest.mark.parametrize("name", known_dialects())
def test_window_probe_orders_by_a_real_column(name):
    """Live-measured trap: MySQL rejects ``OVER (ORDER BY 1)`` outright.

    Error 3592 — "ORDER BY or PARTITION BY uses legacy position indication
    which is unsupported in window functions" — so a positional order-by
    recorded a FALSE "window functions not supported" in the capability
    profile on a MySQL 8.4.5 source. Each probe must also have a FROM.
    """
    sql = profile_for(name).window_probe_sql or ""
    assert sql, f"{name} has no window-function probe"
    assert not re.search(r"OVER\s*\(\s*ORDER\s+BY\s+\d", sql, re.I), (
        f"{name} window probe orders by a position: {sql}"
    )
    assert re.search(r"\bFROM\b", sql, re.I), (
        f"{name} window probe has no FROM clause: {sql}"
    )


def test_unknown_dialect_hint_forbids_vendor_syntax():
    assert "UNKNOWN" in UNKNOWN_DIALECT_HINT
    assert "backticks" in UNKNOWN_DIALECT_HINT.lower()
    for vendor in ("postgres", "mysql", "oracle", "mssql"):
        assert vendor not in UNKNOWN_DIALECT_HINT.lower()
