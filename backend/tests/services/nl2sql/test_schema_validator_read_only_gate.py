"""Write/DDL statements are rejected before any schema resolution runs.

This KB is read-only: INSERT / UPDATE / DELETE / DROP / CREATE / ALTER /
TRUNCATE / MERGE must never reach the connector. The gate runs on the
sqlglot AST inside ``validate_against_schema`` (and defensively in
``db_tools._execute_query``).
"""

import pytest
from unittest.mock import MagicMock

from app.services.nl2sql.schema_validator import (
    _is_write_or_ddl,
    check_read_only_sql,
    validate_against_schema,
)

_WRITE_OR_DDL_SQL = [
    "INSERT INTO users (id, name) VALUES (1, 'a')",
    "UPDATE users SET name = 'x' WHERE id = 1",
    "DELETE FROM users WHERE id = 1",
    "DROP TABLE users",
    "CREATE TABLE tmp (id INT)",
    "ALTER TABLE users ADD COLUMN c INT",
    "TRUNCATE TABLE users",
    "MERGE INTO users USING (SELECT 1 AS id) s ON users.id = s.id WHEN MATCHED THEN DELETE",
]

_READ_ONLY_SQL = [
    "SELECT * FROM users",
    "SELECT id, COUNT(*) FROM users GROUP BY id",
    "WITH x AS (SELECT 1 AS id) SELECT * FROM x",
    "SHOW TABLES",
    "DESCRIBE users",
    "EXPLAIN SELECT * FROM users",
]


def _make_db(kb=None):
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = kb
    db.query.return_value = q
    return db


def _kb(db_type="mysql"):
    kb = MagicMock()
    kb.id = "kb1"
    kb.db_type = db_type
    return kb


@pytest.mark.parametrize("sql", _WRITE_OR_DDL_SQL)
def test_write_or_ddl_rejected_by_validator(sql):
    db = _make_db(kb=_kb())
    result = validate_against_schema(sql, "kb1", db)
    assert result["is_valid"] is False
    joined = " ".join(result["errors"]).lower()
    assert "read-only" in joined or "write" in joined or "ddl" in joined, result


@pytest.mark.parametrize("sql", _WRITE_OR_DDL_SQL)
def test_is_write_or_ddl_detects(sql):
    assert _is_write_or_ddl(sqlglot_parse(sql)) is True


@pytest.mark.parametrize("sql", _READ_ONLY_SQL)
def test_read_only_sql_is_not_write_or_ddl(sql):
    assert _is_write_or_ddl(sqlglot_parse(sql)) is False


def test_check_read_only_sql_rejects_write():
    assert check_read_only_sql("DELETE FROM users") is not None


def test_check_read_only_sql_allows_select():
    assert check_read_only_sql("SELECT * FROM users") is None


def test_check_read_only_sql_allows_introspection():
    assert check_read_only_sql("SHOW TABLES") is None


def sqlglot_parse(sql):
    import sqlglot

    return sqlglot.parse_one(sql, dialect="mysql")
