"""Tests for universal_analytics/query.py — SQL execution engine."""

import pytest
from unittest.mock import MagicMock, patch


class TestQueryValidation:
    def test_validate_sql_allows_select(self):
        from app.services.universal_analytics.query import validate_sql
        assert validate_sql("SELECT * FROM sales", "mysql") is None  # no error
        assert validate_sql("SELECT a FROM t WHERE x=1", "postgres") is None

    def test_validate_sql_rejects_ddl(self):
        from app.services.universal_analytics.query import validate_sql
        error = validate_sql("DROP TABLE users", "mysql")
        assert error is not None
        assert "not allowed" in error.lower() or "only select" in error.lower()

    def test_validate_sql_rejects_insert(self):
        from app.services.universal_analytics.query import validate_sql
        error = validate_sql("INSERT INTO t VALUES (1)", "postgres")
        assert error is not None

    def test_validate_sql_rejects_update(self):
        from app.services.universal_analytics.query import validate_sql
        error = validate_sql("UPDATE t SET x=1", "sqlite")
        assert error is not None

    def test_validate_sql_rejects_delete(self):
        from app.services.universal_analytics.query import validate_sql
        error = validate_sql("DELETE FROM t WHERE id=1", "mysql")
        assert error is not None

    def test_validate_sql_rejects_multiple_statements(self):
        from app.services.universal_analytics.query import validate_sql
        error = validate_sql("SELECT 1; DROP TABLE t", "mysql")
        assert error is not None
