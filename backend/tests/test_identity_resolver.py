"""P3 — cross-source identity resolution: the four tiers.

Non-negotiables pinned here:
- tier 1 (exact) NEVER matches `1001` vs `C1001`
- tier 2 transforms identifiers ONLY with recorded, re-validated evidence
  (value-set containment ≥ 0.9, ≥ 3 shared pairs); no rule → no match
- tiers 3-4 land in the human review queue (inferred), never auto-confirmed
- rejected pairs are remembered and never re-proposed
- bounded sampling; crosswalks never merge or copy source records
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import IdentityMatch
from app.models.knowledge_base import KnowledgeBase

ORG = "org-test"


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


@pytest.fixture()
def two_sources(db):
    """Two staged file sources: a CRM extract and a customer master."""
    from app.services.identity.file_rows import ingest_structured_rows

    import app.services.document_ingestion.service as svc
    from pathlib import Path
    import tempfile

    root = Path(tempfile.mkdtemp())
    svc._upload_root = lambda: root  # module-level patch for this test module

    def _mk(name, sheet, rows):
        from openpyxl import Workbook

        p = root / name
        wb = Workbook()
        wb.remove(wb.active)
        ws = wb.create_sheet(title=sheet)
        for row in rows:
            ws.append(row)
        wb.save(p)
        kb = KnowledgeBase(org_id=ORG, name=name, source_kind="file",
                           file_type="xlsx", file_url=f"/api/uploads/{name}")
        db.add(kb)
        db.commit()
        ingest_structured_rows(db, kb)
        return {"kind": "file", "kb_id": kb.id, "sheet": sheet,
                "column": rows[0][0], "pk_column": rows[0][0]}

    db.expire_all()
    return _mk


# ── Tier 1: exact ───────────────────────────────────────────────────────


def test_tier1_exact_matches_and_never_strips_prefixes(db, two_sources):
    from app.services.identity.resolver import resolve_object_identity

    a = two_sources("crm.xlsx", "CRM", [
        ["account_id", "account_name"],
        [1001, "Acme"], [1002, "Bravo"], [1003, "Charlie"],
    ])
    b = two_sources("master.xlsx", "Master", [
        ["customer_no", "customer_name"],
        ["1001", "Acme"], ["C1002", "Bravo"], ["1004", "Delta"],
    ])
    report = resolve_object_identity(db, ORG, "obj-cust", a, b)
    db.commit()

    assert report["tier1"] == 1  # only 1001 ≡ 1001
    matches = db.query(IdentityMatch).filter_by(object_id="obj-cust").all()
    by_pk = {(m.pk_a, m.pk_b): m for m in matches}
    m = by_pk[("1001", "1001")]
    assert m.match_tier == "exact"
    assert m.status == "inferred"              # system evidence ≠ human decision
    assert m.reviewed_by is None               # reviewed_by reserved for humans
    assert m.confidence == 1.0
    assert ("1002", "C1002") not in by_pk      # prefix ≠ exact
    assert ("1003", "1004") not in by_pk


# ── Tier 2: evidence-backed normalization ───────────────────────────────


def test_tier2_normalization_requires_containment_evidence(db, two_sources):
    from app.services.identity.resolver import resolve_object_identity

    a = two_sources("crm2.xlsx", "CRM", [
        ["account_id", "account_name"],
        [1001, "Acme"], [1002, "Bravo"], [1003, "Charlie"], [1005, "Echo"],
    ])
    b = two_sources("master2.xlsx", "Master", [
        ["customer_no", "customer_name"],
        ["C1001", "Acme"], ["C1002", "Bravo"], ["C1003", "Charlie"],
        ["C1005", "Echo"],
    ])
    report = resolve_object_identity(db, ORG, "obj-c2", a, b)
    db.commit()

    assert report["tier1"] == 0                          # nothing exact
    assert report["tier2"] == 4                          # C1001→1001 etc.
    matches = db.query(IdentityMatch).filter_by(
        object_id="obj-c2", status="inferred").all()
    assert len(matches) == 4
    for m in matches:
        assert m.match_tier == "normalization"
        assert m.reviewed_by is None                     # system ≠ human
        rule = m.normalization_rule_json
        assert rule["transform"] == "strip_alpha_prefix"
        assert rule["containment"] >= 0.9
        assert rule["shared_pairs"] >= 3                 # recorded evidence
        assert m.evidence_json["matched_value"]          # sample provenance


def test_tier1_and_2_not_active_until_human_confirm(db, two_sources):
    """Contract: system matching generates EVIDENCE (inferred, high
    confidence); only a human decision makes a crosswalk ACTIVE.
    read_object_data's cross_sources block must therefore stay empty
    after a resolve and appear only after an explicit confirm."""
    from app.services.identity.resolver import resolve_object_identity
    from app.services.identity.review import crosswalk_summary, review_match

    a = two_sources("crm3.xlsx", "CRM", [
        ["account_id", "account_name"],
        [1001, "Acme"], [1002, "Bravo"], [1003, "Charlie"],
        [1004, "Delta"], [1005, "Echo"],
    ])
    b = two_sources("master3.xlsx", "Master", [
        ["customer_no", "customer_name"],
        ["1001", "Acme"], ["C1002", "Bravo"], ["C1003", "Charlie"],
        ["C1004", "Delta"], ["C1005", "Echo"],
    ])
    report = resolve_object_identity(db, ORG, "obj-gate", a, b)
    db.commit()
    assert report["tier1"] == 1 and report["tier2"] == 4

    # Nothing human-confirmed yet → no active crosswalks.
    assert crosswalk_summary(db, ORG, "obj-gate")["sources"] == []

    # Human confirms ONE pair → exactly that pair becomes active.
    m = db.query(IdentityMatch).filter_by(
        object_id="obj-gate", pk_a="1001").first()
    review_match(db, ORG, m.id, "confirm", reviewed_by="boss")
    s = crosswalk_summary(db, ORG, "obj-gate")
    assert len(s["sources"]) == 1
    assert s["sources"][0]["confirmed_pairs"] == 1
    assert s["sources"][0]["sample"] == [{"pk_a": "1001", "pk_b": "1001"}]


def test_tier2_without_evidence_never_transforms(db, two_sources):
    """One-sided prefix with poor containment → NO match (never assume)."""
    from app.services.identity.resolver import resolve_object_identity

    a = two_sources("crm3.xlsx", "CRM", [
        ["account_id", "account_name"],
        [1001, "Acme"], [1002, "Bravo"], [1003, "Charlie"],
    ])
    b = two_sources("master3.xlsx", "Master", [
        ["customer_no", "customer_name"],
        ["C1001", "Acme"], ["C9999", "Zulu"], ["C8888", "Yankee"],
    ])
    report = resolve_object_identity(db, ORG, "obj-c3", a, b)
    db.commit()
    assert report["tier2"] == 0
    assert db.query(IdentityMatch).filter_by(object_id="obj-c3").count() == 0


# ── Tier 3/4: review queue only ─────────────────────────────────────────


def _bag_embed():
    """Deterministic case-sensitive token-bag embeddings with a vocab
    shared across calls (like a real embedding space). Identical texts
    → cosine 1.0; 'GmbH' vs 'Gmbh' typo → cosine ≈ 0.816 (weak signal)."""
    vocab: dict[str, int] = {}

    def embed(texts):
        for t in texts:
            for tok in t.split():
                vocab.setdefault(tok, len(vocab))
        dim = len(vocab)
        out = []
        for t in texts:
            v = [0.0] * dim
            for tok in t.split():
                v[vocab[tok]] += 1.0
            out.append(v)
        return out

    return embed


def test_tier3_semantic_lands_in_review_queue(db, two_sources, monkeypatch):
    from app.services.identity import resolver
    from app.services.identity.resolver import resolve_object_identity

    a = two_sources("crm4.xlsx", "CRM", [
        ["account_id", "account_name", "city"],
        [1001, "Acme GmbH", "Munich"], [1002, "Bravo Ltd", "Leeds"],
    ])
    b = two_sources("master4.xlsx", "Master", [
        ["customer_no", "customer_name", "city"],
        ["C9001", "Acme GmbH", "Munich"], ["C9002", "Totally Different", "Oslo"],
    ])

    monkeypatch.setattr(resolver, "_embed_texts", _bag_embed())

    report = resolve_object_identity(db, ORG, "obj-c4", a, b,
                                     attribute_pairs=[("account_name", "customer_name"),
                                                      ("city", "city")])
    db.commit()

    assert report["tier3"] >= 1
    m = (db.query(IdentityMatch)
         .filter_by(object_id="obj-c4", match_tier="semantic").first())
    assert m is not None and m.status == "inferred"   # human review required
    assert m.reviewed_by is None
    assert m.confidence >= resolver._SEMANTIC_COSINE


def test_tier4_weak_signal_review_queue(db, two_sources, monkeypatch):
    from app.services.identity import resolver
    from app.services.identity.resolver import resolve_object_identity

    a = two_sources("crm5.xlsx", "CRM", [
        ["account_id", "account_name"],
        [1001, "Acme Handels GmbH"],
    ])
    b = two_sources("master5.xlsx", "Master", [
        ["customer_no", "customer_name"],
        ["C7777", "Acme Handels Gmbh"],   # typo — weak similarity only
    ])

    monkeypatch.setattr(resolver, "_embed_texts", _bag_embed())

    report = resolve_object_identity(db, ORG, "obj-c5", a, b,
                                     attribute_pairs=[("account_name", "customer_name")])
    db.commit()
    m = (db.query(IdentityMatch)
         .filter_by(object_id="obj-c5", match_tier="probabilistic").first())
    assert m is not None and m.status == "inferred"
    assert m.confidence < resolver._SEMANTIC_COSINE
    assert m.confidence >= resolver._PROB_COSINE_MIN


# ── Rejection memory + bounds ───────────────────────────────────────────


def test_rejected_pairs_are_never_reproposed(db, two_sources):
    from app.services.identity.resolver import resolve_object_identity
    from app.services.identity.review import review_match

    a = two_sources("crm6.xlsx", "CRM", [
        ["account_id", "account_name"],
        [1001, "Acme"], [1002, "Bravo"],
    ])
    b = two_sources("master6.xlsx", "Master", [
        ["customer_no", "customer_name"],
        ["1001", "Acme"], ["1002", "Bravo"],
    ])
    resolve_object_identity(db, ORG, "obj-c6", a, b)
    db.commit()
    m = db.query(IdentityMatch).filter_by(object_id="obj-c6").first()
    review_match(db, ORG, m.id, "reject", reviewed_by="boss")

    resolve_object_identity(db, ORG, "obj-c6", a, b)   # re-run
    db.commit()
    statuses = {mm.pk_a: mm.status for mm in
                db.query(IdentityMatch).filter_by(object_id="obj-c6").all()}
    assert statuses[m.pk_a] == "rejected"              # decision remembered
    assert m.reviewed_by == "boss"


def test_db_source_gate_poisoning_does_not_abort_resolution(
        db, two_sources, monkeypatch):
    """A DB-source read whose execution gate swallows a failed internal
    query must NOT poison the request transaction: resolution continues
    and matches are still recorded. (Found live: drifted-schema
    deployments abort the shared session inside QueryService gates.)"""
    import sqlite3
    import tempfile
    from pathlib import Path

    from app.models.knowledge_base import KnowledgeBase
    from app.services.identity.resolver import resolve_object_identity

    # Real DB source: a SQLite file with C-prefixed ids.
    p = Path(tempfile.mkdtemp()) / "p3_poison.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE customers (customer_no TEXT, customer_name TEXT)")
    conn.executemany("INSERT INTO customers VALUES (?, ?)", [
        ("C1001", "Acme"), ("C1002", "Bravo"),
        ("C1003", "Charlie"), ("C1004", "Dx"),
    ])
    conn.commit()
    conn.close()

    db_kb = KnowledgeBase(org_id=ORG, name="p3-poison-db",
                          source_kind="database", db_type="sqlite",
                          api_url=str(p))
    db.add(db_kb)
    db.commit()

    # Poison exactly like the drifted-schema live failure: a gate query
    # fails on the SHARED session, and the gate swallows the exception.
    import app.services.db.execution_gate as gate

    real_enforce = gate.enforce

    def poisoned_enforce(session, *a, **kw):
        from sqlalchemy import text as _t

        try:
            session.execute(_t("SELECT * FROM __no_such_table_p3__"))
        except Exception:
            pass  # swallowed exactly like enforce does
        return real_enforce(session, *a, **kw)

    monkeypatch.setattr(gate, "enforce", poisoned_enforce)

    a = two_sources("crm7.xlsx", "CRM", [
        ["account_id", "account_name"],
        [1001, "Acme"], [1002, "Bravo"], [1003, "Charlie"], [1004, "Dx"],
    ])
    b = {"kind": "db", "kb_id": db_kb.id, "table": "customers",
         "column": "customer_no", "pk_column": "customer_no"}

    report = resolve_object_identity(
        db, ORG, "obj-poison", a, b,
        attribute_pairs=[("account_name", "customer_name")],
    )
    db.commit()
    assert report["tier2"] == 4, report
    m = db.query(IdentityMatch).filter_by(object_id="obj-poison").first()
    assert m is not None and m.status == "inferred"


def test_recovery_preserves_unrelated_pending_change(db, two_sources, monkeypatch):
    """Transaction integrity of the shared-session recovery (closure task 2):
    an unrelated PENDING change in the request session must survive the
    execution-gate poisoning + resolver recovery — the recovery may never
    discard work it does not own."""
    import sqlite3
    import tempfile
    from pathlib import Path

    from app.models.knowledge_base import KnowledgeBase
    from app.models.ontology import OntologyObject
    from app.services.identity.resolver import resolve_object_identity

    # Real DB source (SQLite file) with ids matching the file side exactly
    # → tier 1 candidates.
    p = Path(tempfile.mkdtemp()) / "p3_pending.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE customers (customer_no TEXT)")
    conn.executemany("INSERT INTO customers VALUES (?)",
                     [("1001",), ("1002",), ("1003",), ("1004",)])
    conn.commit()
    conn.close()
    db_kb = KnowledgeBase(org_id=ORG, name="p3-pending-db",
                          source_kind="database", db_type="sqlite",
                          api_url=str(p))
    db.add(db_kb)
    db.commit()

    # The file source KB creation above commits; the UNRELATED change is
    # added now and left UNCOMMITTED (flushed into the outer transaction —
    # exactly the state a request session may carry when resolve runs).
    unrelated = OntologyObject(org_id=ORG, domain_id="d1",
                               name="PendingThing", backing_kind="manual",
                               backing_ref="")
    db.add(unrelated)
    db.flush()
    unrelated_id = unrelated.id

    # Poison exactly like the live failure: a gate query fails on the
    # SHARED session and the gate swallows the exception.
    import app.services.db.execution_gate as gate

    real_enforce = gate.enforce

    def poisoned_enforce(session, *a, **kw):
        from sqlalchemy import text as _t

        try:
            session.execute(_t("SELECT * FROM __no_such_table_p3__"))
        except Exception:
            pass  # swallowed exactly like enforce does
        return real_enforce(session, *a, **kw)

    monkeypatch.setattr(gate, "enforce", poisoned_enforce)

    # Mechanism-level check FIRST, before any commit: the recovery itself
    # must be non-destructive — the unrelated change is still alive and
    # persistent (flushed into the OUTER transaction, untouched by the
    # savepoint recovery) immediately after the poisoned read.
    from sqlalchemy import inspect as sa_inspect

    from app.services.identity import resolver as resolver_mod

    result = resolver_mod._fetch_via_query_service(
        db, db_kb.id, "SELECT customer_no FROM customers", 500,
        ctx="customers")
    assert result["rows"], result
    assert sa_inspect(unrelated).persistent, (
        "recovery discarded unrelated pending work")

    a = two_sources("crm9.xlsx", "CRM", [
        ["account_id"], [1001], [1002], [1003], [1004],
    ])
    b = {"kind": "db", "kb_id": db_kb.id, "table": "customers",
         "column": "customer_no", "pk_column": "customer_no"}

    report = resolve_object_identity(db, ORG, "obj-pending", a, b)
    db.commit()

    assert report["tier1"] == 4, report
    # The unrelated pending change survived the recovery and the commit.
    assert db.get(OntologyObject, unrelated_id) is not None


def test_sampling_is_bounded(db, two_sources):
    from app.services.identity.resolver import resolve_object_identity

    a = two_sources("crm7.xlsx", "CRM", [["account_id"]] +
                    [[i] for i in range(20)])
    b = two_sources("master7.xlsx", "Master", [["customer_no"]] +
                    [[str(i)] for i in range(20)])
    report = resolve_object_identity(db, ORG, "obj-c7", a, b, max_rows=5)
    db.commit()
    assert report["tier1"] <= 5   # only sampled rows participate
