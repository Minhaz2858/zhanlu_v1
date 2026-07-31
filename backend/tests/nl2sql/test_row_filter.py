"""Tests for row-level permission filter injection."""

import pytest
from app.services.nl2sql.row_filter import inject


def test_inject_appends_to_simple_select():
    """Appends a WHERE clause to a SELECT with no existing WHERE."""
    sql = "SELECT id, name FROM customers"
    filters = [{"table": "customers", "filter": "region = 'EU'"}]
    result = inject(sql, filters, "postgresql")
    assert "WHERE" in result
    assert "region = 'EU'" in result
    # Should be valid SQL
    assert "FROM customers WHERE" in result


def test_inject_ands_to_existing_where():
    """ANDs the filter onto an existing WHERE clause."""
    sql = "SELECT id, name FROM customers WHERE active = 1"
    filters = [{"table": "customers", "filter": "region = 'EU'"}]
    result = inject(sql, filters, "postgresql")
    assert "active = 1" in result
    assert "region = 'EU'" in result
    assert "AND" in result


def test_inject_multiple_filters():
    """Appends multiple row-level filters."""
    sql = "SELECT id, name FROM orders"
    filters = [
        {"table": "orders", "filter": "region = 'EU'"},
        {"table": "orders", "filter": "status = 'active'"},
    ]
    result = inject(sql, filters, "postgresql")
    assert "region = 'EU'" in result
    assert "status = 'active'" in result


def test_inject_skips_non_matching_table():
    """Only injects filters for tables actually in the SQL."""
    sql = "SELECT id FROM customers"
    filters = [
        {"table": "orders", "filter": "region = 'EU'"},
    ]
    result = inject(sql, filters, "postgresql")
    # No WHERE should be added for a non-matching table
    assert "WHERE" not in result or "orders" not in result


def test_inject_empty_filters_returns_original():
    sql = "SELECT * FROM customers"
    result = inject(sql, [], "postgresql")
    assert result.strip() == sql.strip()


def test_inject_with_sqlite_dialect():
    sql = 'SELECT "id", "name" FROM "customers"'
    filters = [{"table": "customers", "filter": "region = 'EU'"}]
    result = inject(sql, filters, "sqlite")
    assert "region = 'EU'" in result


def test_inject_preserves_original_sql_structure():
    sql = "SELECT id, name, SUM(amount) FROM orders GROUP BY id, name"
    filters = [{"table": "orders", "filter": "created_at >= '2024-01-01'"}]
    result = inject(sql, filters, "postgresql")
    assert "GROUP BY" in result
    assert "created_at >= '2024-01-01'" in result
