"""Tests for _build_validation_hints — blank_dimension hint + master lookup.

The blank_dimension hint must be independently gated by SCHEMA_GRAPH_ENABLED
(not KG_BUSINESS_CONTEXT_ENABLED), so it can't be silently disabled by the
business-semantic-layer flag.
"""
from unittest.mock import patch

from app.config import settings
from app.services.tool_handlers import db_tools


def _result(rows, sql="SELECT * FROM erp_product_sales_details WHERE ship_date >= '2026-07-01'"):
    return {"rows": rows, "sql": sql}


def _blank_rows():
    return [
        {"FCUSTMATNAME": "", "FMATERIALID": 1, "amount": 100},
        {"FCUSTMATNAME": None, "FMATERIALID": 2, "amount": 200},
        {"FCUSTMATNAME": "   ", "FMATERIALID": 3, "amount": 300},
    ]


def test_blank_dimension_hint_with_master_join(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
    monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", False)
    with patch.object(
        db_tools, "_find_master_for_fk",
        return_value=("erp_t_bd_material", "FMATERIALID", "FMATERIALID"),
    ) as m:
        hints = db_tools._build_validation_hints(None, "kb1", _result(_blank_rows()))
    m.assert_called_once()
    assert len(hints) == 1
    h = hints[0]
    assert h["kind"] == "blank_dimension"
    assert h["column"] == "FCUSTMATNAME"
    assert h["master_table"] == "erp_t_bd_material"
    assert h["join"] == "FMATERIALID -> FMATERIALID"
    assert "erp_t_bd_material" in h["message"]


def test_blank_dimension_hint_disabled_when_schema_graph_off(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", False)
    monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", False)
    hints = db_tools._build_validation_hints(None, "kb1", _result(_blank_rows()))
    assert hints == []


def test_blank_dimension_hint_generic_when_from_parse_fails(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
    monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", False)
    with patch.object(db_tools, "_find_master_for_fk") as m:
        hints = db_tools._build_validation_hints(None, "kb1", _result(_blank_rows(), sql="SELECT 1"))
    m.assert_not_called()
    assert len(hints) == 1
    h = hints[0]
    assert h["kind"] == "blank_dimension"
    assert h["master_table"] is None
    assert "master" in h["message"].lower()


def test_no_hint_when_name_column_has_values(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
    monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", False)
    with patch.object(db_tools, "_find_master_for_fk") as m:
        hints = db_tools._build_validation_hints(
            None, "kb1",
            _result([{"FCUSTMATNAME": "PVC", "FMATERIALID": 1, "amount": 100}]),
        )
    m.assert_not_called()
    assert hints == []


def test_no_hint_on_numeric_only_columns(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
    monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", False)
    hints = db_tools._build_validation_hints(
        None, "kb1",
        _result([{"amount": None, "volume": ""}]),
    )
    assert hints == []


def test_no_hint_on_empty_rows(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
    monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", False)
    hints = db_tools._build_validation_hints(None, "kb1", _result([]))
    assert hints == []


def test_find_master_for_fk_delegates_to_schema_graph(monkeypatch):
    fake = ("erp_t_bd_material", "FMATERIALID", "FMATERIALID")
    with patch.object(db_tools, "SchemaGraph") as MockGraph, \
         patch.object(db_tools, "_relation_partners",
                      return_value=["erp_t_bd_material"]):
        MockGraph.return_value.build.return_value.find_master_for_fk.return_value = fake
        out = db_tools._find_master_for_fk(
            None, "kb1", "erp_product_sales_details", "FMATERIALID"
        )
    MockGraph.return_value.build.assert_called_once()
    assert out == fake


def test_parse_from_table_extracts_bare_and_quoted():
    assert db_tools._parse_from_table(
        "SELECT a FROM erp_product_sales_details WHERE 1=1"
    ) == "erp_product_sales_details"
    assert db_tools._parse_from_table("SELECT 1") is None
    assert db_tools._parse_from_table("") is None
