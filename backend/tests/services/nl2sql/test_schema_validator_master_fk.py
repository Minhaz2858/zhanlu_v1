"""FK-master hints for unknown columns.

When a column is not found in its referenced table, the validator consults
``SchemaGraph.find_master_for_fk``. If the column resolves to an FK on a
master table, it appends a JOIN hint so the agent self-corrects in one retry
(e.g. it guessed ``erp_t_bd_material`` while the catalog knows the master).
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from app.services.nl2sql.schema_validator import validate_against_schema

FLAG = "app.services.nl2sql.schema_validator.settings.SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED"
SCHEMA_GRAPH = "app.services.db.schema_graph.SchemaGraph"

_COLUMNS = {
    "erp_t_sal_outstock": ["FID", "FMATERIALID", "shipment_date", "shipment_quantity"],
    "bd_material": ["FMATERIALID", "FNAME"],
}

_CATALOG_TABLES = list(_COLUMNS.keys())


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


def _graph_with_master(master):
    graph = MagicMock()
    graph.find_master_for_fk.return_value = master
    return graph


def _ctx(graph, svc):
    stack = ExitStack()
    stack.enter_context(
        patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc)
    )
    stack.enter_context(patch(SCHEMA_GRAPH, return_value=graph))
    stack.enter_context(patch(FLAG, True))
    return stack


def test_fk_column_gets_master_hint():
    db = _make_db(kb=_kb())
    svc = _describe_svc(table_columns=_COLUMNS, tables=_CATALOG_TABLES)
    graph = _graph_with_master(("bd_material", "FMATERIALID", "FMATERIALID"))
    with _ctx(graph, svc):
        result = validate_against_schema(
            "SELECT erp_t_sal_outstock.FNAME FROM erp_t_sal_outstock",
            "kb1", db,
        )
    assert result["is_valid"] is False
    text = " ".join(result["available_suggestions"])
    assert "FNAME is an FK to bd_material.FMATERIALID" in text
    assert "JOIN that table first" in text


def test_fk_lookup_called_with_resolved_table_and_column():
    db = _make_db(kb=_kb())
    svc = _describe_svc(table_columns=_COLUMNS, tables=_CATALOG_TABLES)
    graph = _graph_with_master(("bd_material", "FMATERIALID", "FMATERIALID"))
    with _ctx(graph, svc):
        validate_against_schema(
            "SELECT erp_t_sal_outstock.FNAME FROM erp_t_sal_outstock",
            "kb1", db,
        )
    graph.find_master_for_fk.assert_called_once_with(
        "erp_t_sal_outstock", "FNAME"
    )


def test_no_master_found_adds_no_hint():
    db = _make_db(kb=_kb())
    svc = _describe_svc(table_columns=_COLUMNS, tables=_CATALOG_TABLES)
    graph = _graph_with_master(None)
    with _ctx(graph, svc):
        result = validate_against_schema(
            "SELECT erp_t_sal_outstock.FNAME FROM erp_t_sal_outstock",
            "kb1", db,
        )
    text = " ".join(result["available_suggestions"])
    assert "is an FK" not in text  # did-you-mean may still fire, FK hint must not


def test_flag_off_never_builds_graph():
    db = _make_db(kb=_kb())
    svc = _describe_svc(table_columns=_COLUMNS, tables=_CATALOG_TABLES)
    graph = _graph_with_master(("bd_material", "FMATERIALID", "FMATERIALID"))
    with (
        patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc),
        patch(SCHEMA_GRAPH, return_value=graph),
    ):
        result = validate_against_schema(
            "SELECT FNAME FROM erp_t_sal_outstock", "kb1", db
        )
    graph.find_master_for_fk.assert_not_called()
    assert result["available_suggestions"] == []
