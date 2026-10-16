"""P-SV Source Verification Gate tests.

Covers: extended role taxonomy (name hints beat structure), grain inference,
the gate itself (metric-binding auto-repair, archive-twin removal, detail
question vs pre-aggregated sources clarify, forecast exclusion, overlay
overrides), and grain persistence via _persist_catalog.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_catalog import (
    KBTableMeta,
    ProjectCatalogOverlay,
    ProjectMetric,
)

KB = "kb-svg"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _mk_table(db, name, *, role="fact", grain=None, authority=None,
              kb_id=KB):
    db.add(KBTableMeta(
        kb_id=kb_id, schema_name="", table_name=name,
        table_role=role, grain=grain, authority_override=authority,
    ))
    db.commit()


# ── role classification: name hints beat structure ─────────────────────────


def _tbl(name, columns, row_count=50000, fks=None):
    return {
        "table_name": name,
        "row_count": row_count,
        "columns": columns,
        "foreign_keys": fks or [],
    }


def _col(name, dtype="varchar(64)", pk=False):
    return {"column_name": name, "data_type": dtype, "is_primary_key": pk}


def test_name_hints_beat_structure():
    from app.services.knowledge_graph.catalog_indexer import (
        _classify_table_role, _AGGREGATE_RE,
    )

    assert _AGGREGATE_RE.search("sales_summary")
    # Structurally a fact (big, dated, measured) but NAMED a rollup.
    agg = _tbl("erp_sales_summary", [
        _col("id", "int", pk=True), _col("stat_date", "date"),
        _col("amount", "decimal"),
    ])
    assert _classify_table_role(agg, 0) == "aggregate"
    assert _classify_table_role(
        _tbl("v_sale_forecast", [_col("id", "int", pk=True)]), 0
    ) == "forecast"
    assert _classify_table_role(
        _tbl("t_order_archive", [_col("id", "int", pk=True)]), 0
    ) == "archive"
    assert _classify_table_role(
        _tbl("vw_inventory_report", [_col("id", "int", pk=True)]), 0
    ) == "reporting_view"
    # Structural path still works on neutral names.
    assert _classify_table_role(
        _tbl("t_customer", [
            _col("FID", "int", pk=True), _col("FName", "varchar"),
            _col("FType", "varchar"),
        ], row_count=120),
        5,
    ) == "entity_master"


def test_classify_table_roles_sets_grain():
    from app.services.knowledge_graph.catalog_indexer import (
        _classify_table_roles,
    )

    tables = [
        _tbl("t_sale_order_entry", [
            _col("FBILLNO", "varchar"), _col("FDATE", "date"),
            _col("FAMOUNT", "decimal"),
        ]),
        _tbl("t_customer", [
            _col("FID", "int", pk=True), _col("FName", "varchar"),
        ], row_count=100),
    ]
    _classify_table_roles(tables)
    assert tables[0]["grain"] == "one row per fbillno"
    assert tables[1]["grain"] == "one row per fid"


# ── the gate ────────────────────────────────────────────────────────────────


def test_metric_binding_auto_repairs_plan(db):
    _mk_table(db, "erp_t_sale_orderentry", role="fact",
              grain="one row per fbillno")
    _mk_table(db, "erp_v_contract_execution", role="reporting_view")
    db.add(ProjectMetric(
        project_id="p1", kb_id=KB, name="月度销售额",
        aliases=["monthly sales"], status="approved",
        bindings=[{"table": "erp_t_sale_orderentry",
                   "measure_columns": ["amount"]}],
    ))
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(
        db, KB, "what is the total revenue?",  # metric name/alias absent → no match
        ["erp_v_contract_execution"], project_id="p1",
    )
    assert res.passed and not res.added_tables

    res2 = verify_sources(
        db, KB, "show me the monthly sales (月度销售额) trend",
        ["erp_v_contract_execution"], project_id="p1",
    )
    assert "erp_t_sale_orderentry" in res2.added_tables
    assert any("月度销售额" in d for d in res2.disclosures)
    assert "REQUIRED source(s) added" in res2.guidance


def test_archive_twin_of_selected_live_table_is_dropped(db):
    _mk_table(db, "erp_t_sale_order", role="fact")
    _mk_table(db, "erp_t_sale_order_archive", role="archive")
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(db, KB, "total orders", [
        "erp_t_sale_order", "erp_t_sale_order_archive",
    ])
    assert "erp_t_sale_order_archive" in res.removed_tables
    assert "erp_t_sale_order" not in res.removed_tables
    assert any("archived copy" in d for d in res.disclosures)


def test_lone_archive_table_is_kept(db):
    _mk_table(db, "t_price_old", role="archive")
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(db, KB, "total price", ["t_price_old"])
    assert res.removed_tables == []
    assert res.passed


def test_detail_question_on_aggregated_only_clarifies(db):
    _mk_table(db, "sales_summary", role="aggregate",
              grain="one row per product (pre-aggregated) + date")
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(
        db, KB, "列出每笔订单明细 (show order line item detail)",
        ["sales_summary"],
    )
    assert res.clarify
    assert "pre-aggregated" in res.clarify
    assert res.guidance == ""


def test_detail_question_with_fact_table_passes(db):
    _mk_table(db, "sales_summary", role="aggregate")
    _mk_table(db, "t_order_entry", role="fact")
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(db, KB, "list each order line item detail",
                         ["sales_summary", "t_order_entry"])
    assert res.passed


def test_forecast_table_excluded_for_non_forecast_question(db):
    _mk_table(db, "sales_forecast", role="forecast")
    _mk_table(db, "t_sales", role="fact")
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(db, KB, "total sales this month",
                         ["sales_forecast", "t_sales"])
    assert "sales_forecast" in res.removed_tables
    # A forecast question keeps its forecast table.
    res2 = verify_sources(db, KB, "forecast sales next month",
                          ["sales_forecast", "t_sales"])
    assert res2.removed_tables == []


def test_guidance_carries_grain_and_unknown_grain_is_honest(db):
    _mk_table(db, "t_sales", role="fact", grain="one row per order_no")
    _mk_table(db, "t_mystery", role="unknown", grain=None)
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(db, KB, "how much revenue", ["t_sales", "t_mystery"])
    assert "t_sales: grain = one row per order_no" in res.guidance
    assert "grain unknown" in res.guidance
    assert "check the primary key" in res.guidance


def test_overlay_overrides_role_and_grain(db):
    _mk_table(db, "v_orders", role="reporting_view",
              grain="one row per pre-aggregated period")
    db.add(ProjectCatalogOverlay(
        project_id="p1", kb_id=KB, table_name="v_orders",
        table_role="fact", grain="one row per order_no",
    ))
    db.commit()

    from app.services.db.source_verification import verify_sources

    # Detail question: the human override says this IS a fact table, so no
    # clarify fires.
    res = verify_sources(db, KB, "show line item detail", ["v_orders"],
                         project_id="p1")
    assert res.passed
    assert "one row per order_no" in res.guidance

    # Without the project the auto-classification still applies.
    res2 = verify_sources(db, KB, "show line item detail", ["v_orders"])
    assert res2.clarify


def test_authority_override_carried_into_facts(db):
    _mk_table(db, "t_price", role="fact", authority="authoritative")
    db.commit()

    from app.services.db.source_verification import verify_sources

    res = verify_sources(db, KB, "average price", ["t_price"])
    assert res.passed  # smoke: gate reads authority without error


def test_empty_selection_is_a_pass(db):
    from app.services.db.source_verification import verify_sources

    res = verify_sources(db, KB, "anything", [])
    assert res.passed


# ── persistence ─────────────────────────────────────────────────────────────


def test_persist_catalog_writes_grain(db):
    from app.services.knowledge_graph.catalog_indexer import _persist_catalog

    tables = [{
        "table_name": "t_so", "schema_name": "", "table_type": "TABLE",
        "row_count": 10, "table_role": "fact",
        "grain": "one row per order_no",
        "columns": [_col("order_no", "varchar", pk=True)],
        "foreign_keys": [],
    }]
    _persist_catalog(db, KB, tables)
    meta = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == KB, KBTableMeta.table_name == "t_so")
        .first()
    )
    assert meta is not None
    assert meta.grain == "one row per order_no"
