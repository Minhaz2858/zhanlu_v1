"""Introspection statements must pass through the structural validator.

SHOW / DESCRIBE / EXPLAIN / PRAGMA / ANALYZE execute natively on the DB
engine; the validator has nothing structural to check and must not reject
them (it previously reported "no tables referenced" / "table does not
exist" on these statements).
"""

from unittest.mock import MagicMock

from app.services.nl2sql.schema_validator import validate_against_schema


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


def _assert_passthrough(sql, db_type="mysql"):
    db = _make_db(kb=_kb(db_type))
    result = validate_against_schema(sql, "kb1", db)
    assert result["is_valid"] is True, result


def test_show_tables_passthrough():
    _assert_passthrough("SHOW TABLES")


def test_show_tables_like_passthrough():
    _assert_passthrough("SHOW TABLES LIKE '%material%'")


def test_describe_table_passthrough():
    _assert_passthrough("DESCRIBE erp_product")


def test_explain_select_passthrough():
    _assert_passthrough("EXPLAIN SELECT FNAME FROM erp_product")


def test_pragma_table_info_passthrough():
    _assert_passthrough("PRAGMA table_info(users)")


def test_analyze_table_passthrough():
    _assert_passthrough("ANALYZE TABLE users")


def test_show_columns_passthrough():
    _assert_passthrough("SHOW COLUMNS FROM users")


def test_plain_select_is_not_passthrough():
    """Regression guard: real SELECTs still go through structural checks."""
    db = _make_db(kb=_kb())
    result = validate_against_schema("SELECT * FROM erp_product", "kb1", db)
    # Either valid (table resolvable) or invalid with a structural error —
    # but NEVER the introspection passthrough short-circuit.
    assert "is_valid" in result
    assert result["is_valid"] is False  # no SchemaService mock → describe fails
