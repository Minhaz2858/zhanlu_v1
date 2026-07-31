"""Tests for per-dialect quoting rule hints injected into the LLM prompt."""

import pytest
from app.services.nl2sql.dialect_rules import quote_rule


def test_quote_rule_postgres_double_quotes():
    rule = quote_rule("postgres")
    assert '"' in rule
    assert "quote" in rule.lower() or "identifier" in rule.lower()


def test_quote_rule_postgresql_alias_same():
    rule = quote_rule("postgresql")
    assert '"' in rule  # alias for postgres


def test_quote_rule_sqlite_double_quotes():
    rule = quote_rule("sqlite")
    assert '"' in rule


def test_quote_rule_mysql_backticks():
    rule = quote_rule("mysql")
    assert "`" in rule


def test_quote_rule_unknown_falls_back_to_postgres():
    rule = quote_rule("unknown_dialect")
    assert '"' in rule


def test_quote_rule_content_is_non_empty():
    for dialect in ("postgres", "postgresql", "sqlite", "mysql"):
        assert len(quote_rule(dialect)) > 20, f"Rule for {dialect} is too short"
