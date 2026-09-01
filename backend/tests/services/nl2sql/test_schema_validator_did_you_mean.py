"""'Did you mean' fuzzy suggestions for unknown tables/columns.

Fully generic: candidates come from the catalog (``SchemaService.list_tables``
/ ``describe_table``), never from hardcoded vendor names.
"""

from unittest.mock import MagicMock, patch

from app.services.nl2sql.schema_validator import (
    _suggest_table_matches,
    validate_against_schema,
)

FLAG = "app.services.nl2sql.schema_validator.settings.SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED"

_CATALOG_TABLES = [
    "erp_t_sal_outstock",
    "erp_t_sal_outstockentry",
    "bd_material",
    "erp_product_sales_details",
]

_COLUMNS = {
    "erp_product_sales_details": [
        "FMATERIALID", "material_id", "FNAME", "shipment_quantity",
        "contract_price", "partner_name", "shipment_date",
    ],
}


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


def _describe_svc(table_columns=None, tables=None):
    svc = MagicMock()
    table_columns = table_columns or {}

    def _desc(kb_id, table):
        cols = table_columns.get(table, [])
        return {"columns": [{"name": c} for c in cols]}

    svc.describe_table.side_effect = _desc
    if tables is not None:
        svc.list_tables.return_value = {"tables": tables}
    return svc


def _flag_on():
    return patch(FLAG, True)


def test_unknown_table_gets_fuzzy_suggestions():
    db = _make_db(kb=_kb())
    svc = _describe_svc(tables=_CATALOG_TABLES)
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc), _flag_on():
        result = validate_against_schema(
            "SELECT * FROM erp_t_bd_material", "kb1", db
        )
    assert result["is_valid"] is False
    text = " ".join(result["available_suggestions"])
    assert "bd_material" in text  # closest catalog table
    assert "describe_schema" in text


def test_suggest_table_matches_capped_at_five():
    svc = MagicMock()
    close_pool = [
        "erp_t_sal_outstock",
        "erp_t_sal_outstock_2",
        "erp_t_sal_outstock_3",
        "erp_t_sal_outstock_archive",
        "erp_t_sal_outstock_backup",
        "erp_t_sal_outstockentry",
    ]
    svc.list_tables.return_value = {"tables": close_pool}
    matches = _suggest_table_matches(svc, "kb1", "erp_t_sal_outstockx")
    assert len(matches) <= 5
    assert len(matches) == 5  # all six are close → truncated to 5


def test_unknown_column_qualified_gets_fuzzy_suggestions():
    db = _make_db(kb=_kb())
    svc = _describe_svc(table_columns=_COLUMNS, tables=_CATALOG_TABLES)
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc), _flag_on():
        result = validate_against_schema(
            "SELECT erp_product_sales_details.material_name FROM erp_product_sales_details",
            "kb1", db,
        )
    assert result["is_valid"] is False
    text = " ".join(result["available_suggestions"])
    assert "material_id" in text  # fuzzy-close real column


def test_unknown_column_unqualified_gets_fuzzy_suggestions():
    db = _make_db(kb=_kb())
    svc = _describe_svc(table_columns=_COLUMNS, tables=_CATALOG_TABLES)
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc), _flag_on():
        result = validate_against_schema(
            "SELECT material_name FROM erp_product_sales_details", "kb1", db
        )
    assert result["is_valid"] is False
    text = " ".join(result["available_suggestions"])
    assert "material_id" in text


def test_flag_off_returns_no_suggestions():
    """Legacy behavior when SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED is off."""
    db = _make_db(kb=_kb())
    svc = _describe_svc(tables=_CATALOG_TABLES)
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema(
            "SELECT * FROM erp_t_bd_material", "kb1", db
        )
    assert result["is_valid"] is False
    assert result["available_suggestions"] == []


def test_available_columns_still_present_alongside_suggestions():
    """Back-compat: available_columns must survive the new suggestions key."""
    db = _make_db(kb=_kb())
    svc = _describe_svc(table_columns=_COLUMNS, tables=_CATALOG_TABLES)
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc), _flag_on():
        result = validate_against_schema(
            "SELECT erp_product_sales_details.material_name FROM erp_product_sales_details",
            "kb1", db,
        )
    assert "available_columns" in result
    assert set(result["available_columns"].keys()) == {
        "erp_product_sales_details"
    }
    assert "material_id" in result["available_columns"]["erp_product_sales_details"]
