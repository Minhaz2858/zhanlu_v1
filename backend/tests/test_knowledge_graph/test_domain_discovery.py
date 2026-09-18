"""Tests for the Business Domain layer (domain_discovery + column semantics).

Pure-function stages run against synthetic vectors/graphs; the persistence and
scoping stages run against a SQLite catalog fixture. No LLM, no Chroma.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import (
    KBDomain,
    KBDomainMember,
    KBColumnMeta,
    KBTableMeta,
)
from app.services.knowledge_graph import domain_discovery as dd
from app.services.knowledge_graph.catalog_indexer import classify_column_semantics


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_kb(db, name: str = "domains-db") -> KnowledgeBase:
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        app_id="test-app",
        org_id="test-org",
        name=name,
        source_kind="database",
        db_type="postgresql",
        host="localhost",
        database_name="acme",
        catalog_status="ready",
    )
    db.add(kb)
    db.commit()
    return kb


def _make_table(db, kb_id: str, table_name: str) -> KBTableMeta:
    row = KBTableMeta(
        id=str(uuid.uuid4()),
        kb_id=kb_id,
        schema_name="public",
        table_name=table_name,
        table_type="TABLE",
        row_count=100,
        description_en=f"{table_name} description",
        table_role="fact",
    )
    db.add(row)
    db.commit()
    return row


# ── stage 1: structural ────────────────────────────────────────────────────


def test_structural_partition_splits_disconnected_groups():
    tables = ["a", "b", "c", "d"]
    relations = [("a", "b", 1.0), ("c", "d", 1.0)]
    labels = dd.structural_partition(tables, relations)

    assert labels["a"] == labels["b"]
    assert labels["c"] == labels["d"]
    assert labels["a"] != labels["c"]


def test_structural_partition_without_edges_is_unconstrained():
    labels = dd.structural_partition(["a", "b"], [])
    assert set(labels.values()) == {-1}


# ── stage 2: semantic ──────────────────────────────────────────────────────


def test_semantic_partition_finds_two_groups():
    vectors = {
        "a": [1.0, 0.0, 0.0],
        "b": [0.98, 0.05, 0.0],
        "c": [0.0, 1.0, 0.0],
        "d": [0.05, 0.98, 0.0],
    }
    labels = dd.semantic_partition(["a", "b", "c", "d"], vectors)

    assert labels["a"] == labels["b"]
    assert labels["c"] == labels["d"]
    assert labels["a"] != labels["c"]


def test_semantic_partition_is_single_cluster_below_minimum():
    labels = dd.semantic_partition(["a", "b"], {"a": [1.0], "b": [0.0]})
    assert labels == {"a": 0, "b": 0}


def test_semantic_partition_marks_missing_vectors():
    vectors = {f"t{i}": [1.0, float(i)] for i in range(4)}
    labels = dd.semantic_partition([*vectors, "orphan"], vectors)
    assert labels["orphan"] == -1


# ── stage 3: merge engine ──────────────────────────────────────────────────


def test_merge_unites_structural_and_semantic_when_centroids_align():
    tables = ["a", "b", "c", "d"]
    # Structure says {a,b} and {c,d}; semantics says the very same thing.
    structural = {"a": 0, "b": 0, "c": 1, "d": 1}
    semantic = {"a": 0, "b": 0, "c": 1, "d": 1}
    vectors = {
        "a": [1.0, 0.0],
        "b": [1.0, 0.0],
        "c": [0.0, 1.0],
        "d": [0.0, 1.0],
    }

    clusters = dd.merge_partitions(tables, structural, semantic, vectors)
    as_sets = {frozenset(c) for c in clusters}

    assert frozenset({"a", "b"}) in as_sets
    assert frozenset({"c", "d"}) in as_sets


def test_merge_keeps_a_table_in_its_structural_group_when_semantics_disagree():
    tables = ["a", "b", "c", "d"]
    # a and b are joined structurally but their vectors point elsewhere.
    structural = {"a": 0, "b": 0, "c": 1, "d": 1}
    semantic = {"a": 0, "b": 1, "c": 1, "d": 0}
    vectors = {
        "a": [1.0, 0.0],
        "b": [0.0, 1.0],
        "c": [0.0, 1.0],
        "d": [1.0, 0.0],
    }

    clusters = dd.merge_partitions(tables, structural, semantic, vectors, threshold=0.99)

    # Every table is placed exactly once — nothing is dropped.
    placed = sorted(t for c in clusters for t in c)
    assert placed == tables


def test_merge_keeps_unconstrained_tables_as_their_own_cluster():
    clusters = dd.merge_partitions(
        ["a", "b", "c"],
        {"a": 0, "b": 0, "c": -1},
        {"a": 0, "b": 0, "c": -1},
        {"a": [1.0, 0.0], "b": [1.0, 0.0]},
    )
    assert sorted(t for c in clusters for t in c) == ["a", "b", "c"]
    assert ["c"] in clusters


def test_absorb_clusters_folds_a_similar_singleton():
    clusters = [["a", "b", "c"], ["d"]]
    vectors = {
        "a": [1.0, 0.0],
        "b": [1.0, 0.0],
        "c": [1.0, 0.0],
        "d": [1.0, 0.0],
    }
    merged = dd.absorb_clusters(clusters, vectors, threshold=0.75, min_tables=2)
    assert len(merged) == 1
    assert sorted(merged[0]) == ["a", "b", "c", "d"]


def test_absorb_clusters_keeps_a_dissimilar_singleton():
    clusters = [["a", "b", "c"], ["d"]]
    vectors = {
        "a": [1.0, 0.0],
        "b": [1.0, 0.0],
        "c": [1.0, 0.0],
        "d": [0.0, 1.0],
    }
    merged = dd.absorb_clusters(clusters, vectors, threshold=0.75, min_tables=2)
    assert len(merged) == 2


# ── column semantics ───────────────────────────────────────────────────────


def test_classify_column_semantics_covers_the_shapes():
    assert classify_column_semantics(
        {"column_name": "order_id", "data_type": "int", "is_primary_key": True}
    )[0] == "identifier"
    assert classify_column_semantics(
        {"column_name": "created_at", "data_type": "timestamp"}
    )[0] == "temporal"
    assert classify_column_semantics(
        {"column_name": "amount", "data_type": "decimal(18,2)"}
    )[0] == "measure"
    assert classify_column_semantics(
        {"column_name": "is_active", "data_type": "boolean"}
    )[0] == "flag"

    stype, enums = classify_column_semantics(
        {
            "column_name": "order_status",
            "data_type": "varchar(20)",
            "value_samples": ["NEW", "PAID", "SHIPPED"],
        }
    )
    assert stype == "dimension"
    assert enums == ["NEW", "PAID", "SHIPPED"]


def test_classify_column_semantics_withholds_enums_for_high_cardinality():
    samples = [f"value-{i}" for i in range(40)]  # query limit reached → unbounded
    stype, enums = classify_column_semantics(
        {"column_name": "customer_name", "data_type": "varchar(80)", "value_samples": samples}
    )
    assert stype == "dimension"
    assert enums is None


# ── stage 4: naming ────────────────────────────────────────────────────────


class _FakeLLM:
    def __init__(self, payload, explode: bool = False):
        self.payload = payload
        self.explode = explode
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        if self.explode:
            raise RuntimeError("llm down")
        return {"data": self.payload}


@pytest.mark.asyncio
async def test_name_domains_applies_llm_names(monkeypatch):
    rows = [
        [
            KBTableMeta(
                id="1", kb_id="kb", schema_name="s", table_name="sales_orders",
                table_type="TABLE", description_en="orders",
            )
        ]
    ]
    fake = _FakeLLM(
        {
            "domains": [
                {
                    "index": 0,
                    "name_en": "Order Management",
                    "name_zh": "订单管理",
                    "description_en": "Sales orders and their lines.",
                    "description_zh": "销售订单。",
                }
            ]
        }
    )
    monkeypatch.setattr(
        "app.services.llm_service.call_llm", fake, raising=False
    )

    names = await dd.name_domains(rows)
    assert names[0]["name_en"] == "Order Management"
    assert names[0]["source"] == "llm"


@pytest.mark.asyncio
async def test_name_domains_falls_back_to_data_derived_names(monkeypatch):
    rows = [
        [
            KBTableMeta(
                id="1", kb_id="kb", schema_name="s", table_name="sales_orders",
                table_type="TABLE", description_en="orders",
            ),
            KBTableMeta(
                id="2", kb_id="kb", schema_name="s", table_name="sales_returns",
                table_type="TABLE", description_en="returns",
            ),
        ]
    ]
    fake = _FakeLLM({}, explode=True)
    monkeypatch.setattr("app.services.llm_service.call_llm", fake, raising=False)

    names = await dd.name_domains(rows)
    assert names[0]["source"] == "derived"
    assert "Sales" in names[0]["name_en"]


# ── persistence + scoping ──────────────────────────────────────────────────


def test_persist_domains_replaces_previous_map_and_labels_tables(db):
    kb = _make_kb(db)
    a = _make_table(db, kb.id, "sales_orders")
    b = _make_table(db, kb.id, "sales_lines")

    dd.persist_domains(
        db,
        kb.id,
        [[a, b]],
        [{"name_en": "Order Management", "name_zh": "订单管理", "source": "llm"}],
    )
    first = db.query(KBDomain).all()
    assert len(first) == 1
    assert db.query(KBDomainMember).count() == 2
    db.refresh(a)
    assert a.domain_name == "Order Management"

    # A second run must not leave the old map behind (hard replace, no orphans).
    dd.persist_domains(
        db,
        kb.id,
        [[a], [b]],
        [
            {"name_en": "Orders", "source": "llm"},
            {"name_en": "Lines", "source": "llm"},
        ],
    )
    assert db.query(KBDomain).count() == 2
    assert db.query(KBDomainMember).count() == 2
    db.refresh(b)
    assert b.domain_name == "Lines"


@pytest.mark.asyncio
async def test_discover_domains_end_to_end_on_catalog_rows(db, monkeypatch):
    kb = _make_kb(db)
    names = ["sales_orders", "sales_lines", "hr_employees", "hr_payroll"]
    rows = [_make_table(db, kb.id, n) for n in names]

    vectors = {
        rows[0].id: [1.0, 0.0],
        rows[1].id: [1.0, 0.0],
        rows[2].id: [0.0, 1.0],
        rows[3].id: [0.0, 1.0],
    }
    monkeypatch.setattr(dd, "_load_table_vectors", lambda kb_id: vectors)
    monkeypatch.setattr(dd, "_load_relations", lambda db_, kb_id: [
        (rows[0].id, rows[1].id, 1.0),
        (rows[2].id, rows[3].id, 1.0),
    ])
    monkeypatch.setattr(
        "app.services.llm_service.call_llm", _FakeLLM({}, explode=True), raising=False
    )

    result = await dd.discover_domains(db, kb.id, rows)

    assert result["ok"] is True
    assert result["domains"] == 2
    assert db.query(KBDomainMember).count() == 4
    groups = {
        d.name_en: sorted(m.table_name for m in db.query(KBDomainMember).filter_by(domain_id=d.id))
        for d in db.query(KBDomain).all()
    }
    assert sorted(groups.values()) == [["hr_employees", "hr_payroll"], ["sales_lines", "sales_orders"]]


def test_scoped_tables_skips_small_catalogs(db, monkeypatch):
    kb = _make_kb(db)
    _make_table(db, kb.id, "only_table")
    monkeypatch.setattr(
        "app.config.settings.KG_DOMAIN_SCOPING_ENABLED", True, raising=False
    )

    scoped, matched = dd.scoped_tables_for_question(db, [kb.id], "how many orders?")
    assert scoped == set()
    assert matched == []


def test_scoped_tables_returns_domain_members_for_a_large_catalog(db, monkeypatch):
    kb = _make_kb(db)
    rows = [_make_table(db, kb.id, f"sales_fact_{i}") for i in range(50)]
    other = _make_table(db, kb.id, "hr_payroll")

    dd.persist_domains(
        db, kb.id, [rows, [other]], [{"name_en": "Sales", "source": "llm"},
                                     {"name_en": "Payroll", "source": "llm"}]
    )
    monkeypatch.setattr(
        "app.config.settings.KG_DOMAIN_SCOPING_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        "app.config.settings.KG_DOMAIN_SCOPING_MIN_TABLES", 40, raising=False
    )

    scoped, matched = dd.scoped_tables_for_question(db, [kb.id], "how many sales orders?")
    assert "sales_fact_0" in scoped
    assert matched and matched[0]["name_en"] == "Sales"


def test_persist_domains_deduplicates_identical_llm_names(db):
    """Two clusters can come back with the same name; the insert must survive.

    Live failure (2026-09-18): the naming model returned "Sales Order Management"
    for two of twenty clusters, kb_domain is unique on (kb_id, name_en), and the
    UniqueViolation aborted the entire catalog index.
    """
    kb = _make_kb(db)
    a = _make_table(db, kb.id, "sales_orderentry")
    b = _make_table(db, kb.id, "sales_deliverynotice")
    c = _make_table(db, kb.id, "sales_contract")

    dd.persist_domains(
        db,
        kb.id,
        [[a, b], [c]],
        [
            {"name_en": "Sales Order Management", "source": "llm"},
            {"name_en": "Sales Order Management", "source": "llm"},
        ],
    )

    domains = db.query(KBDomain).all()
    names = sorted(d.name_en for d in domains)
    assert len(domains) == 2
    assert len(set(names)) == 2, names
    assert "Sales Order Management" in names
    # The duplicate is disambiguated with its own table, not a generic suffix.
    assert any("sales_contract" in n for n in names), names


def test_persist_domains_survives_a_persistence_failure(db, monkeypatch):
    """A failed domain write must not poison the session or the catalog index."""
    kb = _make_kb(db)
    a = _make_table(db, kb.id, "sales_orders")
    b = _make_table(db, kb.id, "hr_staff")

    calls = {"n": 0}
    real_commit = db.commit

    def _flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated constraint failure")
        return real_commit()

    monkeypatch.setattr(db, "commit", _flaky_commit)

    created = dd.persist_domains(
        db,
        kb.id,
        [[a], [b]],
        [{"name_en": "Sales", "source": "llm"}, {"name_en": "HR", "source": "llm"}],
    )

    assert created == []
    # Session is usable again: the next real query works (no PendingRollbackError).
    monkeypatch.undo()
    assert db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).count() == 2


def test_rank_domains_uses_lexical_overlap_when_embeddings_unavailable(db, monkeypatch):
    kb = _make_kb(db)
    rows = [_make_table(db, kb.id, f"sales_fact_{i}") for i in range(3)]
    other = _make_table(db, kb.id, "hr_payroll")
    dd.persist_domains(
        db, kb.id, [rows, [other]], [{"name_en": "Sales Orders", "source": "llm"},
                                     {"name_en": "Payroll", "source": "llm"}]
    )

    def _no_embeddings(*_a, **_k):
        raise RuntimeError("no embedder")

    monkeypatch.setattr(
        "app.services.document_ingestion.embedder.get_embedding_function",
        _no_embeddings,
        raising=False,
    )

    ranked = dd.rank_domains_for_question(db, [kb.id], "sales by order", top_n=2)
    assert ranked[0]["name_en"] == "Sales Orders"
    assert ranked[0]["overlap"] > 0
