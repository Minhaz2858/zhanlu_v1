"""Tests for per-dialect authoring rules injected into the LLM prompt.

The rules are generated from services/db/dialect.py (the same table the
executors use), so these tests pin two things: the rules describe the dialect
the source actually is, and an unidentified dialect gets NO vendor rules.
"""

import pytest
from app.services.nl2sql.dialect_rules import quote_rule


def test_quote_rule_postgres_double_quotes():
    rule = quote_rule("postgres")
    assert '"' in rule
    assert "quote" in rule.lower() or "identifier" in rule.lower()
    assert "postgres" in rule.lower()


def test_quote_rule_postgresql_alias_same():
    assert quote_rule("postgresql") == quote_rule("postgres")


def test_quote_rule_sqlite_double_quotes():
    assert '"' in quote_rule("sqlite")


def test_quote_rule_mysql_backticks():
    assert "`" in quote_rule("mysql")


def test_quote_rule_oracle_paginates_with_fetch_first():
    """Oracle has no LIMIT — the rule must say FETCH FIRST, not LIMIT."""
    rule = quote_rule("oracle")
    assert "FETCH FIRST" in rule
    assert " LIMIT " not in rule.upper().replace("NO LIMIT", "")


def test_quote_rule_mssql_uses_top_not_limit():
    rule = quote_rule("mssql")
    assert "TOP" in rule
    assert "[users]" in rule


def test_quote_rule_mariadb_is_mysql():
    assert quote_rule("mariadb") == quote_rule("mysql")


def test_quote_rule_unknown_gets_no_vendor_rules():
    """Unknown dialects must not be described as PostgreSQL (old behaviour).

    The old implementation fell back to the PostgreSQL rule text, which is how
    an Oracle SQL Server source was prompted to quote like PostgreSQL.
    """
    rule = quote_rule("unknown_dialect")
    assert "postgres" not in rule.lower()
    assert "UNKNOWN" in rule
    assert "ANSI" in rule or "standard" in rule.lower()


def test_quote_rule_content_is_non_empty():
    for dialect in ("postgres", "postgresql", "sqlite", "mysql", "oracle", "mssql"):
        assert len(quote_rule(dialect)) > 20, f"Rule for {dialect} is too short"
