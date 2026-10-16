"""P-SV2 audit-gap closure tests: query lineage, BusinessRuleExtractor,
and org_id enforcement inside the semantic stores."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.models.semantic_guideline import SemanticGuideline

ORG_A = "org-a"
ORG_B = "org-b"
KB_A = "kb-org-a"
KB_B = "kb-org-b"


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


def _mk_kb(db, kb_id, org_id):
    db.add(KnowledgeBase(id=kb_id, name=kb_id, org_id=org_id, db_type="mysql"))
    db.commit()


def _mk_table(db, kb_id, name, *, org_id, table_type="TABLE", grain=None,
              coverage=None):
    m = KBTableMeta(
        kb_id=kb_id, schema_name="", table_name=name,
        table_role="fact", grain=grain, coverage_json=coverage,
        table_type=table_type, org_id=org_id,
    )
    db.add(m)
    db.commit()
    return m


def _mk_col(db, meta, name, *, org_id, semantic_type="dimension",
            enum_values=None, source_comment=None):
    db.add(KBColumnMeta(
        table_meta_id=meta.id, column_name=name, data_type="varchar",
        ordinal=0, semantic_type=semantic_type, enum_values=enum_values,
        source_comment=source_comment, org_id=org_id,
    ))
    db.commit()


# ── lineage ─────────────────────────────────────────────────────────────────


def test_lineage_extracts_tables_columns_filters():
    from app.services.db.lineage import extract_lineage

    lin = extract_lineage(
        "SELECT o.order_no, SUM(o.amount) FROM erp_t_sal_outstock o "
        "JOIN t_customer c ON o.cust_id = c.id "
        "WHERE o.stat_date >= '2026-01-01' GROUP BY o.order_no"
    )
    assert lin is not None
    assert "erp_t_sal_outstock" in lin["tables"]
    assert "t_customer" in lin["tables"]
    assert set(lin["join_tables"]) >= {"t_customer"}
    assert any("order_no" in c for c in lin["columns"])
    assert any("stat_date" in c for c in lin["filter_columns"])


def test_lineage_regex_fallback_on_unparseable_sql(monkeypatch):
    from app.services.db import lineage as lin_mod

    # Force the sqlglot path to fail → regex fallback still yields tables.
    def _boom(sql, dialect):
        raise ValueError("unparseable")

    monkeypatch.setattr(lin_mod, "_extract_tables_columns", _boom)
    lin = lin_mod.extract_lineage("SELECT * FROM not_even_sql")
    assert lin is not None
    assert lin.get("extraction") == "regex_fallback"
    assert "not_even_sql" in lin["tables"]


def test_lineage_none_for_empty_sql():
    from app.services.db.lineage import extract_lineage

    assert extract_lineage("") is None
    assert extract_lineage(None) is None


def test_format_lineage_block():
    from app.services.db.lineage import format_lineage_block

    assert format_lineage_block(None) == ""
    block = format_lineage_block({"tables": ["t_a", "t_b"], "columns": ["t_a.x"]})
    assert "t_a" in block and "columns" in block


# ── org enforcement in the semantic store ───────────────────────────────────


def test_store_drops_other_org_kbs(db):
    _mk_kb(db, KB_A, ORG_A)
    _mk_kb(db, KB_B, ORG_B)
    _mk_table(db, KB_A, "t_org_a", org_id=ORG_A)
    _mk_table(db, KB_B, "t_org_b", org_id=ORG_B)
    db.commit()

    from app.services.db.semantic_store import get_semantic_store

    store = get_semantic_store(db, [KB_A, KB_B])
    # Cross-org KB is dropped (fail closed), and its table is never served.
    names = {r.table_name for r in store.relations()}
    assert "t_org_a" in names
    assert "t_org_b" not in names
    assert store.org_id == ORG_A


def test_store_org_filter_hides_mismatched_rows(db):
    from app.services.db.semantic_store import SemanticStore

    _mk_kb(db, KB_A, ORG_A)
    _mk_table(db, KB_A, "t_right", org_id=ORG_A)
    _mk_table(db, KB_A, "t_mislabeled", org_id=ORG_B)  # buggy payload row
    db.commit()

    store = SemanticStore(db, [KB_A], org_id=ORG_A)
    names = {r.table_name for r in store.relations()}
    assert "t_right" in names
    assert "t_mislabeled" not in names


def test_store_with_unknown_kbs_is_empty(db):
    from app.services.db.semantic_store import get_semantic_store

    store = get_semantic_store(db, ["kb-does-not-exist"])
    assert store.relations() == []


def test_org_filter_hides_cross_org_guidelines(db):
    from app.services.db.semantic_store import SemanticStore

    _mk_kb(db, KB_A, ORG_A)
    db.add(SemanticGuideline(
        datasource_id=KB_A, kind="policy", title="org A rule",
        content="exclude cancelled", is_enabled=True, org_id=ORG_A,
    ))
    db.add(SemanticGuideline(
        datasource_id=KB_A, kind="policy", title="org B rule",
        content="leak attempt", is_enabled=True, org_id=ORG_B,
    ))
    db.commit()

    store = SemanticStore(db, [KB_A], org_id=ORG_A)
    titles = {g["title"] for g in store.guidelines()}
    assert "org A rule" in titles
    assert "org B rule" not in titles


# ── BusinessRuleExtractor ───────────────────────────────────────────────────


def test_extractor_mines_all_rule_sources(db):
    from app.services.db.business_rule_extractor import extract_business_rules

    _mk_kb(db, KB_A, ORG_A)
    m = _mk_table(
        db, KB_A, "erp_t_sal_outstock", org_id=ORG_A,
        grain="one row per fbillno",
        coverage={"date_column": "stat_date", "min_date": "2024-01-01",
                  "max_date": "2026-09-30"},
    )
    _mk_col(db, m, "FSTATUS", org_id=ORG_A,
            source_comment="0=未审核,1=已审核")
    _mk_col(db, m, "FTYPE", org_id=ORG_A, enum_values=["normal", "return"])
    _mk_view = _mk_table(db, KB_A, "v_sales", org_id=ORG_A, table_type="VIEW")

    report = extract_business_rules(db, KB_A)
    contents = " || ".join(r["content"] for r in report["rules"])
    assert "0=未审核" in contents
    assert "normal, return" in contents
    assert "2024-01-01 ~ 2026-09-30" in contents
    assert "one row per fbillno" in contents
    assert "is a VIEW" in contents
    # every rule cites its source
    assert all(r["source"] for r in report["rules"])


def test_sync_stages_disabled_and_is_idempotent(db):
    from app.services.db.business_rule_extractor import sync_business_rules

    _mk_kb(db, KB_A, ORG_A)
    m = _mk_table(db, KB_A, "t_orders", org_id=ORG_A,
                  grain="one row per order_no")
    _mk_col(db, m, "FSTATUS", org_id=ORG_A, source_comment="0=no,1=yes")

    first = sync_business_rules(db, KB_A)
    assert first["created"] > 0
    rows = (
        db.query(SemanticGuideline)
        .filter(SemanticGuideline.datasource_id == KB_A)
        .all()
    )
    assert all(r.title.startswith("[auto]") for r in rows)
    # Staged disabled — an auto-extracted guess is never an authored rule.
    assert all(not r.is_enabled for r in rows)

    second = sync_business_rules(db, KB_A)
    assert second["created"] == 0
    assert second["skipped"] == first["created"]
    total = (
        db.query(SemanticGuideline)
        .filter(SemanticGuideline.datasource_id == KB_A)
        .count()
    )
    assert total == first["created"]


# ── org stamping on writes ──────────────────────────────────────────────────


def test_save_plan_stamps_org(db):
    from app.services.db.plan_memory import (
        load_plan, question_signature, save_plan,
    )

    _mk_kb(db, KB_A, ORG_A)
    q = "what were total sales in june?"
    ok = save_plan(
        db, KB_A, q,
        tables=["t_orders"], sql="SELECT SUM(x) FROM t_orders",
    )
    assert ok
    plan = load_plan(db, KB_A, q)
    assert plan is not None
    from app.models.query_plan_memory import QueryPlanMemory
    row = db.query(QueryPlanMemory).filter(
        QueryPlanMemory.kb_id == KB_A,
        QueryPlanMemory.signature == question_signature(q),
    ).first()
    assert row is not None
    assert row.org_id == ORG_A
