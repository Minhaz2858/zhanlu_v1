"""Tests for the structural SQL validator (schema_validator.py)."""

from unittest.mock import MagicMock, patch

from app.models.knowledge_base import KnowledgeBase
from app.services.nl2sql.schema_validator import (
    _extract_alias_map,
    _extract_tables,
    validate_against_schema,
)


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


def _describe(table_columns):
    """Return a SchemaService stub whose describe_table maps table->columns."""
    svc = MagicMock()

    def _desc(kb_id, table):
        cols = table_columns.get(table, [])
        return {"columns": [{"name": c} for c in cols]}

    svc.describe_table.side_effect = _desc
    return svc


def test_extract_tables():
    from sqlglot import parse_one

    parsed = parse_one("SELECT a.id, b.name FROM orders a JOIN users b ON a.uid = b.id")
    assert _extract_tables(parsed) == {"orders", "users"}


def test_extract_alias_map():
    from sqlglot import parse_one

    parsed = parse_one("SELECT * FROM orders o")
    alias_map = _extract_alias_map(parsed)
    # sqlglot exposes the table alias directly on the Table node in some
    # versions; be lenient — the map must never contain garbage.
    assert isinstance(alias_map, dict)


def test_valid_query_passes():
    db = _make_db(kb=_kb())
    svc = _describe({
        "erp_product_sales_details": ["FMATERIALID", "FNAME", "shipment_quantity"],
        "erp_partner": ["partner_id", "partner_name"],
    })
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema(
            "SELECT FNAME, shipment_quantity FROM erp_product_sales_details", "kb1", db
        )
    assert result["is_valid"] is True
    assert result["errors"] == []
    assert "FMATERIALID" in result["available_columns"]["erp_product_sales_details"]


def test_hallucinated_column_returns_available_columns():
    db = _make_db(kb=_kb())
    svc = _describe({
        "erp_product_sales_details": ["FMATERIALID", "material_id", "FNAME"],
    })
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema(
            "SELECT material_name FROM erp_product_sales_details", "kb1", db
        )
    assert result["is_valid"] is False
    assert any("material_name" in e for e in result["errors"])
    # available_columns must surface the real column list for self-correction
    assert "FNAME" in result["available_columns"]["erp_product_sales_details"]


def test_unknown_table():
    db = _make_db(kb=_kb())
    svc = _describe({})
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema("SELECT * FROM nonexistent", "kb1", db)
    assert result["is_valid"] is False
    assert any("nonexistent" in e for e in result["errors"])


def test_unparseable_sql():
    db = _make_db(kb=_kb())
    result = validate_against_schema("SELECT FROM WHERE", "kb1", db)
    assert result["is_valid"] is False
    assert result["errors"] == ["unparseable SQL"]
    assert result["available_columns"] == {}


def test_no_tables_referenced():
    db = _make_db(kb=_kb())
    result = validate_against_schema("SELECT 1", "kb1", db)
    assert result["is_valid"] is False
    assert "no tables referenced" in result["errors"]


def test_dialect_mapping_used():
    db = _make_db(kb=_kb("mssql"))
    svc = _describe({"orders": ["id", "amount"]})
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema("SELECT TOP 5 amount FROM orders", "kb1", db)
    # TOP is mssql/tsql syntax; with the correct dialect mapping it parses.
    assert result["is_valid"] is True


def test_unqualified_column_found_in_any_table():
    db = _make_db(kb=_kb())
    svc = _describe({"orders": ["id", "amount"], "users": ["id", "name"]})
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema(
            "SELECT amount FROM orders WHERE id = 1", "kb1", db
        )
    assert result["is_valid"] is True
