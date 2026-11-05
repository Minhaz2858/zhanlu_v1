"""SemanticStore facade tests — tiers, scope isolation, retrieval, workload.

These prove the PROPERTIES the Oracle-style architecture is bought for:

* provenance tiers exist and runtime inference is never promoted,
* retrieval narrows to a small candidate set,
* nothing outside the caller's binding can be read (cross-source leak = 0),
* workload is ranking evidence only and cannot promote an unknown table,
* the store performs no writes at all.

Wiring (which call site consumes which method) is NOT proven here — that needs
the live path; see the skill's verification ladder.
"""

import json
import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest

from app.database import Base, SessionLocal, engine
from app.models.knowledge_catalog import (
    KBColumnMeta,
    KBDomain,
    KBDomainMember,
    KBTableMeta,
    KBTableRelation,
    ProjectCatalogOverlay,
    ProjectMetric,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.query_plan_memory import QueryPlanMemory
from app.models.terminology import Terminology
from app.services.db.semantic_store import (
    TIER_DISCOVERED,
    TIER_LEARNED,
    TIER_RUNTIME,
    TIER_VERIFIED,
    SemanticStore,
    format_retrieval_hint,
    format_source_ranking,
    get_semantic_store,
)
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ── builders ───────────────────────────────────────────────────────────────
def _kb(db, name):
    kb = KnowledgeBase(
        id=str(uuid.uuid4()), name=name, source_kind="database",
        db_type="mysql", database_name=f"db_{name}",
    )
    db.add(kb)
    db.commit()
    return kb.id


def _table(db, kb_id, table_name, description="", domain=None, role="fact"):
    t = KBTableMeta(
        id=str(uuid.uuid4()), kb_id=kb_id, schema_name="", table_name=table_name,
        table_type="TABLE", row_count=100, description_en=description,
        description_zh=None, domain_name=domain, table_role=role,
    )
    db.add(t)
    db.commit()
    return t.id


def _column(db, table_meta_id, name, semantic_type="measure", unit=None, enum=None):
    c = KBColumnMeta(
        id=str(uuid.uuid4()), table_meta_id=table_meta_id, column_name=name,
        ordinal=0, data_type="decimal", is_nullable=True, is_primary_key=False,
        description_en=f"{name} meaning", semantic_type=semantic_type,
        unit=unit, enum_values=enum,
    )
    db.add(c)
    db.commit()
    return c.id


def _relation(db, kb_id, src_meta_id, tgt_meta_id):
    db.add(
        KBTableRelation(
            id=str(uuid.uuid4()), kb_id=kb_id, source_table_meta_id=src_meta_id,
            target_table_meta_id=tgt_meta_id, relation_type="FK",
            source_columns=["id"], target_columns=["table_id"], confidence=1.0,
        )
    )
    db.commit()


# ── tiers ──────────────────────────────────────────────────────────────────
def test_overlay_alias_upgrades_a_relation_to_verified(db):
    kb = _kb(db, "tier-kb")
    _table(db, kb, "orders", description="orders table")
    db.add(
        ProjectCatalogOverlay(
            id=str(uuid.uuid4()), project_id="proj-1", kb_id=kb,
            table_name="orders", alias="sales orders", description="curated",
            scope="table",
        )
    )
    db.commit()

    store = SemanticStore(db, [kb], project_id="proj-1")
    rels = {r.table_name: r for r in store.relations()}
    assert rels["orders"].tier == TIER_VERIFIED
    assert rels["orders"].aliases == ("sales orders",)
    # a curated alias must win over the indexer description
    assert rels["orders"].description == "curated"

    # without the project scope the same table is merely DISCOVERED
    bare = SemanticStore(db, [kb]).relations()[0]
    assert bare.tier == TIER_DISCOVERED
    assert bare.description == "orders table"


def test_ranking_results_are_runtime_inferred(db):
    kb = _kb(db, "tier-rt")
    _table(db, kb, "invoices", description="invoice header")
    store = SemanticStore(db, [kb])
    hits = store.rank_relations("show me invoices", k=5)
    assert hits and all(h.tier == TIER_RUNTIME for h in hits)


# ── scope isolation ────────────────────────────────────────────────────────
def test_store_cannot_read_outside_its_binding(db):
    kb_a = _kb(db, "iso-a")
    kb_b = _kb(db, "iso-b")
    a_meta = _table(db, kb_a, "alpha_table", description="alpha")
    b_meta = _table(db, kb_b, "beta_table", description="beta")
    _column(db, b_meta, "secret_col")
    db.add(
        KBDomain(
            id=str(uuid.uuid4()), kb_id=kb_b, name_en="Secret Domain",
            description_en="x", member_count=1,
        )
    )
    db.add(
        QueryPlanMemory(
            id=str(uuid.uuid4()), kb_id=kb_b, signature="sig-b",
            question="beta question", tables=["beta_table"], sql="select 1",
        )
    )
    db.commit()

    store = SemanticStore(db, [kb_a])
    names = [r.table_name for r in store.relations()]
    assert names == ["alpha_table"]
    assert store.columns("beta_table") == []
    assert store.domains() == []
    assert store.sample_values("beta_table") == {}
    assert store.joins("beta_table") == []
    # workload is scoped too: kb_b's plan memory is invisible
    assert store.workload() == {}
    # columns of an in-scope table still work
    _column(db, a_meta, "amount")
    assert [c.column_name for c in store.columns("alpha_table")] == ["amount"]


def test_empty_scope_returns_nothing(db):
    kb = _kb(db, "empty-scope")
    _table(db, kb, "t", description="d")
    store = SemanticStore(db, [])
    assert store.relations() == []
    assert store.domains() == [] and store.joins() == []
    assert store.glossary("anything") == []
    assert store.rank_sources("anything") == []
    assert store.stats()["relations"] == 0


# ── glossary / domain knowledge ────────────────────────────────────────────
def test_glossary_is_matched_and_verifies_only(db):
    kb = _kb(db, "gloss-kb")
    db.add_all([
        Terminology(id=str(uuid.uuid4()), word="ARR", description="annual recurring revenue"),
        Terminology(id=str(uuid.uuid4()), word="dormant", description="x", enabled=False),
        Terminology(id=str(uuid.uuid4()), word="other-agent-term", description="x",
                    agent_id="someone-else"),
        ProjectMetric(id=str(uuid.uuid4()), project_id="p1", kb_id=kb, name="Net Sales",
                      definition="gross minus returns", unit="元", status="approved",
                      aliases=json.dumps(["revenue"])),
        ProjectMetric(id=str(uuid.uuid4()), project_id="p1", kb_id=kb, name="Draft Metric",
                      definition="not yet approved", status="proposed"),
    ])
    db.commit()

    store = SemanticStore(db, [kb], project_id="p1")
    all_terms = {t.term for t in store.glossary()}
    assert "ARR" in all_terms
    assert "Net Sales" in all_terms
    assert "dormant" not in all_terms          # disabled
    assert "other-agent-term" not in all_terms  # another agent's vocabulary
    assert "Draft Metric" not in all_terms      # not approved -> not verified truth

    matched = store.glossary("what was ARR last month")
    assert [t.term for t in matched] == ["ARR"]
    assert matched[0].tier == TIER_VERIFIED

    # aliases AND definitions are matchable, so a user who says "revenue"
    # reaches the approved metric that lists it as an alias, and the glossary
    # entry whose own definition mentions it.
    via_alias = {t.term for t in store.glossary("revenue please")}
    assert "Net Sales" in via_alias


def test_glossary_respects_datasource_scope(db):
    kb_in = _kb(db, "gloss-in")
    kb_out = _kb(db, "gloss-out")
    db.add(
        Terminology(
            id=str(uuid.uuid4()), word="仅限此源", description="scoped",
            datasource_ids=json.dumps([kb_in]),
        )
    )
    db.add(
        MetricDefinition(
            id=str(uuid.uuid4()), name="GMV", datasource_id=kb_out,
            description="gross merchandise value", is_enabled=True,
        )
    )
    db.commit()

    in_store = SemanticStore(db, [kb_in])
    assert "仅限此源" in {t.term for t in in_store.glossary()}
    assert "GMV" not in {t.term for t in in_store.glossary()}

    out_store = SemanticStore(db, [kb_out])
    assert "GMV" in {t.term for t in out_store.glossary()}
    assert "仅限此源" not in {t.term for t in out_store.glossary()}


# ── workload ───────────────────────────────────────────────────────────────
def test_workload_counts_only_tables_in_scope(db):
    kb = _kb(db, "wl-kb")
    _table(db, kb, "orders", description="orders")
    _table(db, kb, "customers", description="customers")
    db.add_all([
        Nl2sqlQueryLog(
            id=str(uuid.uuid4()), question="q1", outcome="success",
            generated_sql="SELECT SUM(x) FROM orders JOIN customers ON 1=1",
        ),
        Nl2sqlQueryLog(
            id=str(uuid.uuid4()), question="q2", outcome="error",
            generated_sql="SELECT * FROM other_warehouse_table",
        ),
    ])
    db.commit()

    wl = SemanticStore(db, [kb]).workload()
    assert set(wl) == {"orders", "customers"}   # the foreign table is ignored
    assert wl["orders"].uses == 1
    assert wl["orders"].successes == 1
    assert wl["orders"].tier == TIER_LEARNED


def test_workload_cannot_create_a_candidate_by_itself(db):
    """Popularity re-ranks retrieval; it never becomes the retrieval."""
    kb = _kb(db, "wl-only")
    _table(db, kb, "hot_table", description="nothing about the question")
    db.add_all([
        Nl2sqlQueryLog(
            id=str(uuid.uuid4()), question=f"q{i}", outcome="success",
            generated_sql="SELECT * FROM hot_table",
        )
        for i in range(20)
    ])
    db.commit()

    store = SemanticStore(db, [kb])
    assert store.workload()["hot_table"].uses == 20   # the signal exists
    # ...and produces nothing on its own
    assert store.rank_relations("completely unrelated wording", k=10) == []
    assert format_retrieval_hint(
        store.rank_relations("completely unrelated wording", k=10)
    ) == ""
    # but it still boosts a table meaning already found
    hits = store.rank_relations("show me hot table", k=10)
    assert hits and hits[0].table_name == "hot_table"
    assert any(e == "uses:20" for e in hits[0].evidence)


def test_workload_reads_verified_plans(db):
    kb = _kb(db, "wl-plan")
    _table(db, kb, "contracts", description="contracts")
    db.add(
        QueryPlanMemory(
            id=str(uuid.uuid4()), kb_id=kb, signature="sig", question="q",
            tables=json.dumps(["contracts"]), sql="select 1",
        )
    )
    db.commit()
    wl = SemanticStore(db, [kb]).workload()
    assert wl["contracts"].uses == 1 and wl["contracts"].successes == 1


# ── retrieval ──────────────────────────────────────────────────────────────
def test_exact_name_match_outranks_popularity(db):
    kb = _kb(db, "rank-kb")
    _table(db, kb, "match_me_table", description="the right one")
    _table(db, kb, "popular_table", description="unrelated")
    # make the unrelated table overwhelmingly popular
    db.add_all([
        Nl2sqlQueryLog(
            id=str(uuid.uuid4()), question=f"q{i}", outcome="success",
            generated_sql="SELECT * FROM popular_table",
        )
        for i in range(10)
    ])
    db.commit()

    hits = SemanticStore(db, [kb]).rank_relations("show me match me table", k=5)
    assert hits[0].table_name == "match_me_table"
    assert any(e.startswith("name:") for e in hits[0].evidence)
    # the popular table is still returned, just behind
    names = [h.table_name for h in hits]
    assert "popular_table" in names
    assert any(e.startswith("uses:") for e in
               [e for h in hits if h.table_name == "popular_table" for e in h.evidence])


def test_retrieval_expands_join_neighbours(db):
    kb = _kb(db, "join-kb")
    head = _table(db, kb, "contract_header", description="contracts")
    child = _table(db, kb, "line_items", description="line rows")
    _table(db, kb, "unrelated_ref", description="codes")
    _relation(db, kb, head, child)

    hits = SemanticStore(db, [kb]).rank_relations("contracts", k=10)
    names = [h.table_name for h in hits]
    assert "contract_header" in names
    # reachable through the FK graph even though its own name/description never
    # matched the question — at a discount, and carrying the reason.
    assert "line_items" in names, names
    line = [h for h in hits if h.table_name == "line_items"][0]
    assert any(e.startswith("joins:") for e in line.evidence), line.evidence
    assert "unrelated_ref" not in names


def test_retrieval_is_empty_without_a_usable_question(db):
    kb = _kb(db, "rank-empty")
    _table(db, kb, "t", description="d")
    store = SemanticStore(db, [kb])
    assert store.rank_relations("") == []
    assert store.rank_relations("???") == []


# ── cross-source retrieval (the G1 gap) ────────────────────────────────────
def test_rank_sources_picks_the_matching_warehouse(db):
    kb_erp = _kb(db, "erp-warehouse")
    kb_hr = _kb(db, "hr-warehouse")
    _table(db, kb_erp, "erp_sales_order", description="sales order fact")
    _table(db, kb_hr, "hr_employee", description="employee roster")

    store = SemanticStore(db, [kb_erp, kb_hr])
    ranked = store.rank_sources("total sales order value", k=3)
    assert ranked[0].kb_id == kb_erp
    assert any("erp_sales_order" in e for e in ranked[0].evidence)
    assert all(c.tier == TIER_RUNTIME for c in ranked)

    # a question that names neither source has nothing to discriminate
    assert SemanticStore(db, [kb_erp, kb_hr]).rank_sources("hello there") == []
    assert format_source_ranking(
        SemanticStore(db, [kb_erp, kb_hr]).rank_sources("hello there")
    ) == ""

    # one matching source out of two IS the discrimination — it renders
    block = format_source_ranking(store.rank_sources("total sales order value"))
    assert "erp-warehouse" in block
    assert "hr-warehouse" not in block


def test_rank_sources_uses_domain_vocabulary(db):
    kb = _kb(db, "dom-kb")
    t1 = _table(db, kb, "t1", description="x", domain="Market Price Data")
    dom = KBDomain(id=str(uuid.uuid4()), kb_id=kb, name_en="Market Price Data",
                   description_en="prices", member_count=1)
    db.add(dom)
    db.add(KBDomainMember(id=str(uuid.uuid4()), domain_id=dom.id, kb_id=kb,
                          table_meta_id=t1, table_name="t1"))
    db.commit()
    ranked = SemanticStore(db, [kb]).rank_sources("market price trend")
    assert ranked and "domains:Market Price Data" in ranked[0].evidence


# ── prompt rendering ───────────────────────────────────────────────────────
def test_format_helpers(db):
    kb_a = _kb(db, "fmt-a")
    kb_b = _kb(db, "fmt-b")
    _table(db, kb_a, "orders", description="orders")
    _table(db, kb_b, "people", description="people")
    store = SemanticStore(db, [kb_a, kb_b])

    block = format_source_ranking(store.rank_sources("orders count"))
    assert "Datasource match for this question" in block
    assert "orders" in block or "fmt-a" in block

    hint = format_retrieval_hint(store.rank_relations("orders", k=3))
    assert hint.startswith("## Candidate tables for this question")
    assert "orders" in hint

    # empty in, empty out — callers can prepend unconditionally
    assert format_retrieval_hint([]) == ""
    assert format_source_ranking([]) == ""


# ── invariants ─────────────────────────────────────────────────────────────
def test_store_writes_nothing(db):
    kb = _kb(db, "ro-kb")
    _table(db, kb, "t", description="d")
    store = SemanticStore(db, [kb])
    # exercise every read path
    store.relations()
    store.columns("t")
    store.sample_values("t")
    store.joins()
    store.domains()
    store.glossary("q")
    store.workload()
    store.rank_relations("t")
    store.rank_sources("t")
    store.stats()
    assert list(db.new) == [] and list(db.dirty) == [] and list(db.deleted) == []


def test_stats_reports_the_foundation(db):
    kb = _kb(db, "stat-kb")
    t = _table(db, kb, "t", description="described", domain="D")
    _table(db, kb, "u", description="")
    # P0-5 (2026-10-30): columns_described now counts COLUMN rows with
    # descriptions (the old implementation counted described TABLES, which
    # misreported column coverage by ~15x); the table count moved to
    # tables_described.
    _column(db, t, "amount")
    s = SemanticStore(db, [kb]).stats()
    assert s["relations"] == 2
    assert s["columns_described"] == 1
    assert s["tables_described"] == 1
    assert TIER_RUNTIME in s["tiers"]


def test_get_semantic_store_honours_the_flag(monkeypatch, db):
    from app.config import settings

    kb = _kb(db, "flag-kb")
    _table(db, kb, "t", description="d")
    monkeypatch.setattr(settings, "SEMANTIC_STORE_ENABLED", False, raising=False)
    empty = get_semantic_store(db, [kb])
    assert empty.relations() == []
    monkeypatch.setattr(settings, "SEMANTIC_STORE_ENABLED", True, raising=False)
    assert len(get_semantic_store(db, [kb]).relations()) == 1


def test_workload_can_be_disabled(monkeypatch, db):
    from app.config import settings

    kb = _kb(db, "wl-off")
    _table(db, kb, "orders", description="orders")
    db.add(Nl2sqlQueryLog(id=str(uuid.uuid4()), question="q", outcome="success",
                          generated_sql="SELECT * FROM orders"))
    db.commit()
    monkeypatch.setattr(settings, "SEMANTIC_STORE_WORKLOAD_ENABLED", False,
                        raising=False)
    store = SemanticStore(db, [kb])
    assert store.workload() == {}
    hits = store.rank_relations("orders", k=3)  # still ranks on meaning alone
    assert hits and hits[0].table_name == "orders"
    assert not any(e.startswith("uses:") for e in hits[0].evidence)
