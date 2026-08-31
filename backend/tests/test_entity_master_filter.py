"""Entity Master Filter — comprehensive tests for the 7-layer stack.

Layers covered:
  1. Model + migration   (table_role / entity_master_hints columns + migration 066)
  2. Auto-classification (_classify_table_role / _classify_table_roles +
                          _persist_catalog persistence)
  3. Prompt block        (_ENTITY_MASTER_FILTER_BLOCK + get_system_prompt injection)
  4. Schema graph        (TableNode.table_role + _render_node + build())
  5. KG caching          (_agent_kb_ids + _inject_entity_masters)
  6. Verification gate   (_detect_overscope_filter + evaluate_answer wiring)
  7. describe_schema     (_annotate_table_roles)

All expectations are structural — zero hardcoded domain table/column names.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, ProjectCatalogOverlay
from app.services import answer_verification as av
from app.services.agent_prompts import (
    _ENTITY_MASTER_FILTER_BLOCK,
    _agent_is_db_bound,
    get_system_prompt,
)
from app.services.db.schema_graph import SchemaGraph, TableNode
from app.services.dynamic_prompt_builder import (
    _agent_kb_ids,
    _inject_entity_masters,
)
from app.services.knowledge_graph.catalog_indexer import (
    _classify_table_role,
    _classify_table_roles,
    _persist_catalog,
)
from app.services.tool_handlers.db_tools import _annotate_table_roles


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite file per test (mirrors test_catalog_indexer)."""
    import uuid

    from app.database import Base

    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_kb(db) -> KnowledgeBase:
    kb = KnowledgeBase(id="kb1", name="kb1")
    db.add(kb)
    db.commit()
    return kb


class _App:
    """Minimal stand-in for AgentApp (plain attrs, no auto-MagicMock attrs)."""

    def __init__(self, knowledge_bases=None, skills=None):
        self.knowledge_bases = knowledge_bases or []
        self.skills = skills or []
        self.tool_config = {}


def _col(name: str, dtype: str = "VARCHAR") -> dict:
    return {
        "column_name": name,
        "ordinal": 1,
        "data_type": dtype,
        "is_nullable": True,
        "is_primary_key": False,
        "default_value": None,
        "description_zh": "",
        "description_en": "",
    }


def _table(name: str, row_count: int, columns: list[dict], fks: list[dict] | None = None) -> dict:
    return {
        "schema_name": "",
        "table_name": name,
        "table_type": "TABLE",
        "row_count": row_count,
        "columns": columns,
        "foreign_keys": list(fks or []),
    }


def _meta_row(meta_id: str, name: str, role: str, hints: dict | None = None):
    m = MagicMock()
    m.id = meta_id
    m.table_name = name
    m.table_role = role
    m.entity_master_hints = hints or {}
    return m


def _overlay_row(table_name: str, role: str):
    o = MagicMock()
    o.table_name = table_name
    o.table_role = role
    return o


def _role_db(metas, overlays=()):
    """MagicMock db whose query() returns the right rows per model."""
    db = MagicMock()

    def query_fn(model):
        q = MagicMock()
        if model is KBTableMeta:
            q.filter.return_value.all.return_value = list(metas)
            q.filter.return_value.order_by.return_value.all.return_value = list(metas)
        elif model is ProjectCatalogOverlay:
            q.filter.return_value.all.return_value = list(overlays)
            q.filter.return_value.order_by.return_value.all.return_value = list(overlays)
        return q

    db.query.side_effect = query_fn
    return db


# ---------------------------------------------------------------------------
# Layer 1 — model + migration
# ---------------------------------------------------------------------------


def test_kbtablemeta_has_role_columns():
    cols = {c.name: c for c in KBTableMeta.__table__.columns}
    assert "table_role" in cols
    assert "entity_master_hints" in cols
    assert cols["table_role"].default is not None  # python-side default "unknown"
    assert cols["entity_master_hints"].nullable is True


def test_project_catalog_overlay_has_role_column():
    cols = {c.name: c for c in ProjectCatalogOverlay.__table__.columns}
    assert "table_role" in cols
    assert cols["table_role"].nullable is True


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/066_entity_master_table_role.py"
    )
    spec = importlib.util.spec_from_file_location("migration_066", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_066_adds_columns(tmp_path):
    """Run migration 066's upgrade() against an isolated SQLite engine."""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    db_file = tmp_path / "mig066.db"
    engine = sa.create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    md = sa.MetaData()
    sa.Table(
        "kb_table_meta",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("kb_id", sa.String),
        sa.Column("schema_name", sa.String),
        sa.Column("table_name", sa.String),
        sa.Column("table_type", sa.String),
        sa.Column("row_count", sa.Integer),
    )
    sa.Table(
        "project_catalog_overlay",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("project_id", sa.String),
        sa.Column("kb_id", sa.String),
        sa.Column("table_name", sa.String),
        sa.Column("alias", sa.String),
        sa.Column("description", sa.Text),
        sa.Column("metric_definition", sa.Text),
        sa.Column("scope", sa.String(20)),
    )
    md.create_all(engine)

    conn = engine.connect()
    try:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            _load_migration().upgrade()
    finally:
        conn.close()

    insp = sa.inspect(engine)
    kb_cols = {c["name"] for c in insp.get_columns("kb_table_meta")}
    ov_cols = {c["name"] for c in insp.get_columns("project_catalog_overlay")}
    assert "table_role" in kb_cols
    assert "entity_master_hints" in kb_cols
    assert "table_role" in ov_cols
    engine.dispose()


# ---------------------------------------------------------------------------
# Layer 2 — structural auto-classification
# ---------------------------------------------------------------------------


def test_classify_entity_master_with_category():
    t = _table(
        "products", 500,
        [_col("product_id", "INT"), _col("product_name"), _col("category")],
    )
    assert _classify_table_role(t, 0) == "entity_master"


def test_classify_entity_master_via_fk_reference():
    t = _table("materials", 100, [_col("material_id", "INT"), _col("material_name")])
    assert _classify_table_role(t, 3) == "entity_master"
    # Without category AND without FK references it cannot be a master.
    assert _classify_table_role(t, 0) != "entity_master"


def test_classify_fact():
    t = _table(
        "sales_details", 120_000,
        [
            _col("sale_id", "INT"),
            _col("shipment_date", "DATE"),
            _col("quantity", "DECIMAL"),
            _col("amount", "DECIMAL"),
        ],
    )
    assert _classify_table_role(t, 0) == "fact"


def test_classify_bridge():
    t = _table(
        "order_products", 80_000,
        [_col("order_id", "INT"), _col("product_id", "INT"), _col("created_at", "DATETIME")],
    )
    assert _classify_table_role(t, 0) == "bridge"


def test_classify_dimension():
    t = _table("departments", 50, [_col("dept_id", "INT"), _col("dept_name"), _col("location")])
    assert _classify_table_role(t, 0) == "dimension"


def test_classify_unknown_fallback():
    t = _table("logs", 500, [_col("log_id", "INT"), _col("message")])
    assert _classify_table_role(t, 0) == "unknown"


def test_classify_kingdee_fprefix_compound_names():
    """F-prefixed columns (FCUSTID/FNAME/FGROUP) classify as entity_master.

    The keyword is a suffix (``FCUSTID`` -> ``id``) with no leading separator,
    so only the suffix-anchored patterns can see it.  ``FGROUP`` still hits the
    unanchored category pattern (``group``).
    """
    t = _table(
        "erp_t_bd_customer", 771,
        [_col("FCUSTID", "INT"), _col("FNAME"), _col("FGROUP")],
    )
    assert _classify_table_role(t, 0) == "entity_master"


def test_classify_bare_concatenated_id_name():
    """Bare concatenations (CUSTID/CUSTNAME) work with no separators at all."""
    t = _table(
        "customers", 300,
        [_col("CUSTID", "INT"), _col("CUSTNAME"), _col("REGION")],
    )
    assert _classify_table_role(t, 2) == "entity_master"
    # No category and no FK references -> dimension, not entity_master.
    assert _classify_table_role(t, 0) == "dimension"


def test_classify_pascal_case_compound_names():
    """PascalCase (OrderId/ProductName/Category) works via suffix anchor."""
    t = _table(
        "order_items", 500,
        [_col("OrderId", "INT"), _col("ProductName"), _col("Category")],
    )
    assert _classify_table_role(t, 0) == "entity_master"


def test_classify_camel_case_compound_names():
    """CamelCase (materialID/materialName) classifies as dimension."""
    t = _table(
        "materials", 200,
        [_col("materialID", "INT"), _col("materialName")],
    )
    assert _classify_table_role(t, 0) == "dimension"


def test_classify_table_roles_uses_fk_indegree():
    products = _table(
        "products", 500,
        [_col("product_id", "INT"), _col("product_name"), _col("category")],
    )
    details = _table(
        "sales_details", 120_000,
        [
            _col("sale_id", "INT"),
            _col("shipment_date", "DATE"),
            _col("quantity", "DECIMAL"),
            _col("amount", "DECIMAL"),
        ],
        fks=[{"column": "product_id", "ref_table": "products"}],
    )
    _classify_table_roles([details, products])
    roles = {t["table_name"]: t["table_role"] for t in [details, products]}
    assert roles["products"] == "entity_master"
    assert roles["sales_details"] == "fact"


def test_persist_catalog_persists_table_role(db):
    kb = _make_kb(db)
    tables = [
        _table(
            "products", 500,
            [_col("product_id", "INT"), _col("product_name"), _col("category")],
        )
    ]
    tables[0]["table_role"] = "entity_master"
    _persist_catalog(db, kb.id, tables)

    meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).first()
    assert meta is not None
    assert meta.table_role == "entity_master"


def test_persist_catalog_defaults_unknown(db):
    kb = _make_kb(db)
    tables = [_table("logs", 500, [_col("log_id", "INT"), _col("message")])]
    _persist_catalog(db, kb.id, tables)
    meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).first()
    assert meta.table_role == "unknown"


# ---------------------------------------------------------------------------
# Layer 3 — prompt block
# ---------------------------------------------------------------------------


def test_entity_master_block_constant_content():
    assert "ENTITY MASTER FILTER RULE" in _ENTITY_MASTER_FILTER_BLOCK
    assert "table_role: entity_master" in _ENTITY_MASTER_FILTER_BLOCK
    assert "FORBIDDEN" in _ENTITY_MASTER_FILTER_BLOCK
    # Zero hardcoded table names / domain vocabulary. Generic entity nouns
    # (product/customer/supplier) are allowed — they are role-neutral.
    assert "erp_" not in _ENTITY_MASTER_FILTER_BLOCK
    assert "sales_details" not in _ENTITY_MASTER_FILTER_BLOCK


def test_agent_is_db_bound():
    assert _agent_is_db_bound("data_agent", None) is True
    assert _agent_is_db_bound("data_agent", None) is True
    assert _agent_is_db_bound("general_assistant", None) is True
    assert _agent_is_db_bound("custom_agent", _App()) is False
    # User-created agent with a bound knowledge base is db-bound.
    assert _agent_is_db_bound("custom_agent", _App(knowledge_bases=["kb1"])) is True


def test_get_system_prompt_injects_block_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", True)
    prompt = get_system_prompt("data_agent")
    assert "ENTITY MASTER FILTER RULE" in prompt


def test_get_system_prompt_omits_block_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", False)
    prompt = get_system_prompt("data_agent")
    assert "ENTITY MASTER FILTER RULE" not in prompt


def test_get_system_prompt_omits_for_non_db_agent(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", True)
    prompt = get_system_prompt("custom_agent", _App())
    assert "ENTITY MASTER FILTER RULE" not in prompt


# ---------------------------------------------------------------------------
# Layer 4 — schema graph rendering
# ---------------------------------------------------------------------------


def test_tablenode_default_role():
    assert TableNode(name="x").table_role == "unknown"


def test_render_node_includes_role():
    sg = SchemaGraph(None, "kb1")
    node = TableNode(name="products", table_role="entity_master")
    rendered = sg._render_node(node, with_samples=False)
    assert "table_role: entity_master" in rendered


class _FakeConn:
    dialect = "mysql"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        return []


def _sg_db(metas=None, rels=None, kb=None):
    db = MagicMock()

    def query_fn(model):
        q = MagicMock()
        if model is KnowledgeBase:
            q.filter.return_value.first.return_value = kb
        elif model is KBTableMeta:
            q.filter.return_value.all.return_value = metas or []
        elif model is ProjectCatalogOverlay:
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = query_fn
    return db


def _sg_kb():
    kb = MagicMock()
    kb.id = "kb1"
    kb.db_type = "mysql"
    kb.database_name = "warehouse"
    return kb


def test_build_populates_table_role_from_meta():
    db = _sg_db(
        kb=_sg_kb(),
        metas=[_meta_row("m1", "products", "entity_master")],
    )
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": []}
        graph = SchemaGraph(db, "kb1").build(["products"])

    assert graph.nodes["products"].table_role == "entity_master"
    ctx = graph.to_llm_context(token_budget=100_000)
    assert "table_role: entity_master" in ctx


def test_build_keeps_unknown_without_meta():
    db = _sg_db(kb=_sg_kb(), metas=[])
    with patch("app.services.db.schema_graph.SchemaService") as MockSvc, \
         patch("app.services.db.schema_graph.get_connector", return_value=_FakeConn()):
        MockSvc.return_value.describe_table.return_value = {"columns": []}
        graph = SchemaGraph(db, "kb1").build(["products"])

    assert graph.nodes["products"].table_role == "unknown"


# ---------------------------------------------------------------------------
# Layer 5 — knowledge-graph caching (cached master map injection)
# ---------------------------------------------------------------------------


def test_agent_kb_ids_normalization():
    assert _agent_kb_ids(None) == []
    assert _agent_kb_ids(_App()) == []
    assert _agent_kb_ids(_App(knowledge_bases=["kb1", "kb2"])) == ["kb1", "kb2"]
    app_json = _App()
    app_json.knowledge_bases = '["kb1","kb3"]'
    assert _agent_kb_ids(app_json) == ["kb1", "kb3"]
    app_bad = _App()
    app_bad.knowledge_bases = "not-json"
    assert _agent_kb_ids(app_bad) == []


def test_inject_entity_masters_appends_map(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", True)
    db = _role_db(
        [
            _meta_row(
                "m1", "products", "entity_master",
                {
                    "entity_type": "product",
                    "filter_columns": ["FNAME"],
                    "category_column": "FNAME",
                    "sample_categories": ["碳五石油树脂", "双环戊二烯"],
                },
            )
        ]
    )
    out = _inject_entity_masters("base prompt", db, _App(knowledge_bases=["kb1"]), project_id="p1")
    assert "Known Entity Masters" in out
    assert "products" in out
    assert "FNAME" in out
    assert "base prompt" in out


def test_inject_entity_masters_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", False)
    db = _role_db([_meta_row("m1", "products", "entity_master")])
    assert _inject_entity_masters("p", db, _App(knowledge_bases=["kb1"])) == "p"


def test_inject_entity_masters_honors_overlay_override(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", True)
    db = _role_db(
        [_meta_row("m1", "products", "entity_master")],
        overlays=[_overlay_row("products", "dimension")],
    )
    out = _inject_entity_masters("p", db, _App(knowledge_bases=["kb1"]), project_id="p1")
    assert "Known Entity Masters" not in out


def test_inject_entity_masters_no_rows(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", True)
    out = _inject_entity_masters("p", _role_db([]), _App(knowledge_bases=["kb1"]))
    assert out == "p"


# ---------------------------------------------------------------------------
# Layer 6 — verification gate (overscope detector)
# ---------------------------------------------------------------------------


def test_overscope_detector_fires_on_category_dump():
    assert av._detect_overscope_filter(
        "Show me the C5/C9 products",
        [{"row_count": 42_993, "sql": "SELECT * FROM sales_details"}],
    ) is True


def test_overscope_detector_requires_scope_signal():
    assert av._detect_overscope_filter(
        "List all rows in sales",
        [{"row_count": 42_993, "sql": "SELECT * FROM sales_details"}],
    ) is False


def test_overscope_detector_requires_large_dump():
    assert av._detect_overscope_filter(
        "premium customers",
        [{"row_count": 12, "sql": "SELECT * FROM customers"}],
    ) is False


def test_overscope_detector_ignores_filtered_sql():
    assert av._detect_overscope_filter(
        "premium customers",
        [{"row_count": 5_000, "sql": "SELECT * FROM orders WHERE customer_id IN (1,2,3)"}],
    ) is False


def test_overscope_detector_empty_inputs():
    assert av._detect_overscope_filter("", []) is False
    assert av._detect_overscope_filter("C5/C9 products", []) is False


def test_evaluate_answer_flags_overscope(monkeypatch):
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    res = av.evaluate_answer(
        "Show me the C5/C9 products sales",
        [
            {
                "row_count": 42_993,
                "sql": "SELECT * FROM sales_details",
                "response": "Here are 42993 records",
            }
        ],
        "I found 42993 records",
    )
    assert res.status == "INCOMPLETE"
    assert "overscope" in res.signals
    assert any("unfiltered" in g for g in res.gaps)


# ---------------------------------------------------------------------------
# Layer 7 — describe_schema annotation
# ---------------------------------------------------------------------------


def test_annotate_table_roles_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", False)
    result = {"table": "products", "columns": []}
    out = _annotate_table_roles(result, MagicMock(), "kb1")
    assert out is result
    assert "table_role" not in out


def test_annotate_table_roles_describe_table(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", True)
    db = _role_db([_meta_row("m1", "products", "entity_master")])
    out = _annotate_table_roles({"table": "products", "columns": []}, db, "kb1")
    assert out["table_role"] == "entity_master"


def test_annotate_table_roles_describe_all(monkeypatch):
    monkeypatch.setattr(settings, "ENTITY_MASTER_FILTER_ENABLED", True)
    db = _role_db(
        [
            _meta_row("m1", "products", "entity_master"),
            _meta_row("m2", "sales_details", "fact"),
        ]
    )
    result = {
        "tables": [
            {"table": "products", "columns": []},
            {"table": "sales_details", "columns": []},
        ]
    }
    out = _annotate_table_roles(result, db, "kb1")
    roles = {e["table"]: e["table_role"] for e in out["tables"]}
    assert roles == {"products": "entity_master", "sales_details": "fact"}