"""Tests for SchemaGraph — runtime structural view + join planning."""

from unittest.mock import MagicMock, patch

from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, KBTableRelation
from app.services.db.schema_graph import SchemaGraph, _map_dialect, TableNode


class _FakeConn:
    dialect = "mysql"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        if "TABLE_ROWS" in sql:
            return [{"TABLE_ROWS": 12345}]
        if sql.lstrip().upper().startswith("SELECT * FROM"):
            return [{"FMATERIALID": "1001", "qty": 3}]
        return []


def _make_db(metas=None, rels=None, kb=None):
    db = MagicMock()

    def query_fn(model):
        q = MagicMock()
        if model is KnowledgeBase:
            q.filter.return_value.first.return_value = kb
        elif model is KBTableMeta:
            q.filter.return_value.all.return_value = metas or []
        elif model is KBTableRelation:
            q.filter.return_value.all.return_value = rels or []
        return q

    db.query.side_effect = query_fn
    return db


def _kb():
    kb = MagicMock()
    kb.id = "kb1"
    kb.db_type = "mysql"
    kb.database_name = "warehouse"
    return kb


def _meta(mid, name):
    m = MagicMock()
    m.id = mid
    m.table_name = name
    return m


def _rel(src, tgt, kind="FK", conf=1.0, src_cols=None, tgt_cols=None):
    r = MagicMock()
    r.source_table_meta_id = src
    r.target_table_meta_id = tgt
    r.relation_type = kind
    r.confidence = conf
    r.source_columns = src_cols or ["src_col"]
    r.target_columns = tgt_cols or ["tgt_col"]
    return r


def test_map_dialect():
    assert _map_dialect("postgresql") == "postgres"
    assert _map_dialect("mysql") == "mysql"
    assert _map_dialect("mssql") == "tsql"
    assert _map_dialect(None) == "mysql"


def test_build_populates_nodes():
    db = _make_db(kb=_kb())
    columns = [
        {"name": "FMATERIALID", "type": "int", "pk": True, "nullable": False},
        {"name": "shipment_quantity", "type": "decimal", "pk": False, "nullable": True},
    ]
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": columns}
        graph = SchemaGraph(db, "kb1").build(["erp_product_sales_details"])

    node = graph.nodes["erp_product_sales_details"]
    assert node.name == "erp_product_sales_details"
    assert node.columns == columns
    assert node.row_count_approx == 12345
    assert node.sample_rows == [{"FMATERIALID": "1001", "qty": 3}]


def test_get_related_tables_ranking():
    db = _make_db(
        kb=_kb(),
        metas=[_meta("m_a", "a"), _meta("m_b", "b"), _meta("m_c", "c")],
        rels=[
            _rel("m_a", "m_b", kind="NAME_MATCH", conf=0.5),
            _rel("m_a", "m_c", kind="VALUE_OVERLAP", conf=0.9),
        ],
    )
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": []}
        graph = SchemaGraph(db, "kb1").build(["a", "b", "c"])

    related = graph.get_related_tables("a")
    kinds = [e["kind"] for e in related]
    assert kinds[0] == "VALUE_OVERLAP"
    assert kinds[1] == "NAME_MATCH"


def test_to_llm_context_budget_trims_samples():
    db = _make_db(kb=_kb())
    columns = [{"name": "col1", "type": "int", "pk": True}]
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": columns}
        graph = SchemaGraph(db, "kb1").build(["a"])

    full = graph.to_llm_context(token_budget=100000)
    assert "sample_rows" in full

    lean = graph.to_llm_context(token_budget=10)
    assert "sample_rows" not in lean
    assert "Table: a" in lean


def test_focus_table_first():
    db = _make_db(kb=_kb())
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": []}
        graph = SchemaGraph(db, "kb1").build(["a", "b"])

    ctx = graph.to_llm_context(focus_table="b", token_budget=100000)
    assert ctx.index("Table: b") < ctx.index("Table: a")


def test_get_related_tables_empty_for_unknown():
    db = _make_db(kb=_kb())
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": []}
        graph = SchemaGraph(db, "kb1").build(["a"])

    assert graph.get_related_tables("nope") == []


# -- find_master_for_fk -------------------------------------------------


def _meta_with_role(mid, name, role):
    m = _meta(mid, name)
    m.table_role = role
    return m


def _build_graph(db, tables, metas=None):
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": []}
        return SchemaGraph(db, "kb1").build(tables)


def test_find_master_for_fk_hits_entity_master():
    db = _make_db(
        kb=_kb(),
        metas=[
            _meta_with_role("m_a", "a", "fact"),
            _meta_with_role("m_b", "b", "entity_master"),
        ],
        rels=[
            _rel("m_a", "m_b", kind="FK",
                  src_cols=["FMATERIALID"], tgt_cols=["id"]),
        ],
    )
    graph = _build_graph(db, ["a", "b"])
    assert graph.find_master_for_fk("a", "FMATERIALID") == ("b", "id", "FMATERIALID")


def test_find_master_for_fk_none_when_fk_not_in_edge_columns():
    db = _make_db(
        kb=_kb(),
        metas=[
            _meta_with_role("m_a", "a", "fact"),
            _meta_with_role("m_b", "b", "entity_master"),
        ],
        rels=[
            _rel("m_a", "m_b", kind="FK",
                  src_cols=["OTHER_COL"], tgt_cols=["id"]),
        ],
    )
    graph = _build_graph(db, ["a", "b"])
    assert graph.find_master_for_fk("a", "FMATERIALID") is None


def test_find_master_for_fk_none_when_target_not_master():
    db = _make_db(
        kb=_kb(),
        metas=[
            _meta_with_role("m_a", "a", "fact"),
            _meta_with_role("m_b", "b", "dimension"),
        ],
        rels=[
            _rel("m_a", "m_b", kind="FK",
                  src_cols=["FMATERIALID"], tgt_cols=["id"]),
        ],
    )
    graph = _build_graph(db, ["a", "b"])
    assert graph.find_master_for_fk("a", "FMATERIALID") is None


def test_find_master_for_fk_none_for_unknown_table():
    db = _make_db(kb=_kb(), metas=[_meta_with_role("m_a", "a", "fact")])
    graph = _build_graph(db, ["a"])
    assert graph.find_master_for_fk("nope", "col") is None
