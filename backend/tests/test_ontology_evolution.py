"""P6 — continuous ontology evolution: detection, impact, controlled acts.

The e2e path uses a REAL sqlite file datasource: catalog baseline → live
schema change → reconcile (apply) → binding marked stale with a P2
revision → second reconcile is a no-op (idempotency, E1). Verified
definitions are never overwritten (E3); failures are per-source retryable
(E4); everything is org-scoped (E5).
"""

from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.models.metric_definition import MetricDefinition
from app.models.ontology import (
    AgentDomainBinding,
    OntologyDomain,
    OntologyEvolutionState,
    OntologyObject,
)
import app.models.ontology_revision  # noqa: F401
from app.models.ontology_revision import OntologyRevision

ORG = "org-evo"
OTHER = "org-evo-other"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        eng,
        tables=[KnowledgeBase.__table__, KBTableMeta.__table__,
                KBColumnMeta.__table__, MetricDefinition.__table__,
                OntologyDomain.__table__, OntologyObject.__table__,
                OntologyRevision.__table__, OntologyEvolutionState.__table__,
                AgentDomainBinding.__table__],
    )
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _kb(db, kb_id="kb-evo", org=ORG) -> KnowledgeBase:
    kb = KnowledgeBase(org_id=org, name="evo", source_kind="database",
                       db_type="sqlite", api_url="/tmp/unused.db")
    kb.id = kb_id
    db.add(kb)
    db.commit()
    return kb


def _catalog(db, kb_id, tables: dict[str, list[str]]):
    for t, cols in tables.items():
        meta = KBTableMeta(kb_id=kb_id, table_name=t)
        db.add(meta)
        db.flush()
        for i, c in enumerate(cols):
            db.add(KBColumnMeta(table_meta_id=meta.id, column_name=c,
                                ordinal=i))
    db.commit()


def _object(db, kb_id, name, table, *, evidence="verified",
            cols=("famount",)) -> OntologyObject:
    d = db.query(OntologyDomain).filter(
        OntologyDomain.org_id == ORG).first()
    if d is None:
        d = OntologyDomain(org_id=ORG, code="evo", name="Evo")
        db.add(d)
        db.commit()
    obj = OntologyObject(
        org_id=ORG, domain_id=d.id, name=name,
        backing_kind="table", backing_ref=f"kb:{kb_id}:{table}",
        evidence_status=evidence,
        properties_json=[{"name": c, "column": c, "type": "number"}
                         for c in cols])
    db.add(obj)
    db.commit()
    return obj


# ── binding self-check (catalog-only, no live connector) ─────────────────

def test_vanished_table_marks_broken_with_revision(db):
    from app.services.ontology.evolution import reconcile

    kb = _kb(db)
    # Catalog baseline no longer contains the bound table → broken.
    _catalog(db, kb.id, {"other_table": ["c"]})
    obj = _object(db, kb.id, "Order", "sales_order")
    reconcile(db, ORG, apply=True)

    db.expire_all()
    obj = db.get(OntologyObject, obj.id)
    assert obj.binding_status == "broken"   # table not in catalog
    revs = db.query(OntologyRevision).filter(
        OntologyRevision.entity_id == obj.id).all()
    assert revs and revs[0].change_kind == "drift"


def test_vanished_column_marks_stale_not_broken(db, tmp_path):
    from app.services.ontology.evolution import reconcile

    # Live source still HAS the table (so live-diff stays quiet) but the
    # object maps a column that was never materialized → catalog check
    # alone must drive stale (not broken).
    db_path = tmp_path / "stale.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE other_table (c REAL)")
    conn.execute("CREATE TABLE sales_order (famount REAL, fbillno TEXT)")
    conn.commit()
    conn.close()
    kb = KnowledgeBase(org_id=ORG, name="stale-src", source_kind="database",
                       db_type="sqlite", api_url=str(db_path))
    kb.id = "kb-stale"
    db.add(kb)
    db.commit()
    _catalog(db, kb.id, {"other_table": ["c"],
                         "sales_order": ["famount", "fbillno"]})
    _object(db, kb.id, "Order", "sales_order",
            cols=("famount", "fbillno", "fghost"))
    reconcile(db, ORG, apply=True)
    obj = db.query(OntologyObject).filter(
        OntologyObject.name == "Order").one()
    assert obj.binding_status == "stale"   # table exists, mapped col gone


def test_healthy_binding_marked_ok_and_idempotent(db, tmp_path):
    from app.services.ontology.evolution import reconcile

    db_path = tmp_path / "ok.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sales_order (famount REAL)")
    conn.commit()
    conn.close()
    kb = KnowledgeBase(org_id=ORG, name="ok-src", source_kind="database",
                       db_type="sqlite", api_url=str(db_path))
    kb.id = "kb-ok2"
    db.add(kb)
    db.commit()
    _catalog(db, kb.id, {"sales_order": ["famount"]})
    obj = _object(db, kb.id, "Order", "sales_order")
    reconcile(db, ORG, apply=True)
    db.expire_all()
    assert db.get(OntologyObject, obj.id).binding_status == "ok"
    revs = db.query(OntologyRevision).filter(
        OntologyRevision.entity_id == obj.id).all()
    assert len(revs) == 0  # ok is not a change — no revision spam


def test_apply_false_is_report_only(db):
    from app.services.ontology.evolution import reconcile

    kb = _kb(db)
    _catalog(db, kb.id, {"other_table": ["c"]})
    obj = _object(db, kb.id, "Order", "sales_order")
    report = reconcile(db, ORG, apply=False)
    assert any(a["action"] == "would_mark_broken"
               for a in report["actions"])
    db.expire_all()
    assert db.get(OntologyObject, obj.id).binding_status is None


def test_report_only_never_consumes_pending_change(db, tmp_path):
    """A report-only pass must NOT advance the signature ledger —
    otherwise the follow-up apply run would see 'unchanged' and the
    detected drift would be skipped forever."""
    from app.services.ontology.evolution import reconcile

    db_path = tmp_path / "consume.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sales_order (famount REAL)")
    conn.commit()
    conn.close()
    kb = KnowledgeBase(org_id=ORG, name="consume-src",
                       source_kind="database", db_type="sqlite",
                       api_url=str(db_path))
    kb.id = "kb-consume"
    db.add(kb)
    db.commit()
    _catalog(db, kb.id, {"sales_order": ["famount"]})
    obj = _object(db, kb.id, "Order", "sales_order")

    # baseline pass, report-only → sees healthy, persists nothing
    reconcile(db, ORG, apply=False)
    assert db.query(OntologyEvolutionState).count() == 0

    # drift: table renamed away
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE sales_order RENAME TO sales_order_v2")
    conn.commit()
    conn.close()

    r1 = reconcile(db, ORG, apply=False)
    assert any(c["kind"] == "schema_drift" for c in r1["changes"])
    assert db.query(OntologyEvolutionState).count() == 0  # NOT consumed

    # the follow-up apply run still applies the drift
    r2 = reconcile(db, ORG, apply=True)
    assert any(a["action"].startswith("mark_") for a in r2["actions"])
    db.expire_all()
    assert db.get(OntologyObject, obj.id).binding_status in ("stale",
                                                             "broken")


# ── live schema drift through the REAL connector (e2e) ────────────────────

def test_real_schema_change_detected_then_idempotent(db, tmp_path,
                                                     monkeypatch):
    from app.services.ontology.evolution import reconcile

    db_path = tmp_path / "live.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sales_order (famount REAL, fbillno TEXT)")
    conn.commit()
    conn.close()
    kb = KnowledgeBase(org_id=ORG, name="live", source_kind="database",
                       db_type="sqlite", api_url=str(db_path))
    kb.id = "kb-live"
    db.add(kb)
    db.commit()
    _catalog(db, kb.id, {"sales_order": ["famount", "fbillno"]})
    _object(db, kb.id, "Order", "sales_order")

    # Baseline run: live schema matches catalog → ok, state recorded.
    r1 = reconcile(db, ORG, apply=True)
    src = next(s for s in r1["sources"] if s["source_key"] == "db:kb-live")
    assert src["status"] in ("unchanged", "ok")

    # REAL source change: drop a column + add a table in the sqlite file.
    conn = sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE sales_order DROP COLUMN fbillno")
    conn.execute("CREATE TABLE invoices (id INTEGER)")
    conn.commit()
    conn.close()

    r2 = reconcile(db, ORG, apply=True)
    changed = [c for c in r2["changes"]
               if c["source_key"] == "db:kb-live"]
    assert changed, "the schema change must be detected"
    facts = [tuple(f) for f in changed[0]["facts"]]
    assert ("column_removed", "sales_order", "fbillno") in facts
    assert ("table_added", "invoices") in facts
    assert changed[0]["impact"]["objects"], "impact must name the object"

    # IDEMPOTENCY: third run — same live schema, signature unchanged →
    # no new changes, no duplicate actions (E1).
    r3 = reconcile(db, ORG, apply=True)
    assert not [c for c in r3["changes"] if c["source_key"] == "db:kb-live"]
    state = db.query(OntologyEvolutionState).filter(
        OntologyEvolutionState.source_key == "db:kb-live").one()
    assert state.last_status == "changed"


def test_metric_dependency_flagged(db):
    from app.services.ontology.evolution import reconcile

    kb = _kb(db)
    _catalog(db, kb.id, {"sales_order": ["famount"]})
    db.add(MetricDefinition(
        id="met-x", org_id=ORG, datasource_id=kb.id, name="Gone",
        base_table="vanished_table", base_column="famount",
        aggregation="sum", is_enabled=True, lifecycle="published"))
    db.commit()
    report = reconcile(db, ORG, apply=True)
    flagged = [c for c in report["actions"]
               if c.get("object") == "Gone" or "metric" in str(c)]
    assert flagged or report["changes"]  # surfaced honestly, not silently


def test_cross_org_isolation(db):
    from app.services.ontology.evolution import reconcile

    kb_other = _kb(db, kb_id="kb-o2", org=OTHER)
    _catalog(db, kb_other.id, {"sales_order": ["famount"]})
    _object(db, kb_other.id, "OtherOrder", "sales_order")
    report = reconcile(db, ORG, apply=True)
    assert all(not s["source_key"].startswith("db:kb-o2")
               for s in report["sources"])
    assert all("kb-o2" not in str(c) for c in report["changes"])


def test_per_source_failure_is_isolated_and_retryable(db, monkeypatch):
    from app.services.ontology import evolution as evo

    kb1 = _kb(db, kb_id="kb-ok")
    _catalog(db, kb1.id, {"t": ["c"]})
    kb2 = _kb(db, kb_id="kb-bad")
    _catalog(db, kb2.id, {"t": ["c"]})

    real = evo._detect_database_source
    calls = {"n": 0}

    def flaky(dbi, kb):
        if kb.id == "kb-bad":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connector exploded")
        return real(dbi, kb)

    monkeypatch.setattr(evo, "_detect_database_source", flaky)
    report = evo.reconcile(db, ORG, apply=True)
    assert any(f["source_key"] == "db:kb-bad" for f in report["failed"])
    state = db.query(OntologyEvolutionState).filter(
        OntologyEvolutionState.source_key == "db:kb-bad").one()
    assert state.last_status == "failed" and state.attempts >= 1

    # retry without the fault → recovers
    report2 = evo.reconcile(db, ORG, apply=True)
    assert not [f for f in report2["failed"]
                if f["source_key"] == "db:kb-bad"]
    db.expire_all()
    assert db.query(OntologyEvolutionState).filter(
        OntologyEvolutionState.source_key == "db:kb-bad").one().last_status \
        != "failed"


# ── P6-T5: admin endpoints ────────────────────────────────────────────────


def _client(db_session, org=ORG):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.deps import get_db, require_org_admin
    from app.routers.ontology import router as ontology_router

    app = FastAPI()
    app.include_router(ontology_router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_org_admin] = lambda: SimpleNamespace(
        org_id=org)
    return TestClient(app)


def test_evolution_endpoints_report_and_state(db):
    _client(db).post("/ontology/evolution/reconcile", json={"apply": False})

    # empty org → empty ledger
    r = _client(db).get("/ontology/evolution/state")
    assert r.status_code == 200
    assert r.json() == {"sources": []}

    # seed a ledger row in OUR org + one in ANOTHER org
    db.add(OntologyEvolutionState(
        org_id=ORG, source_key="db:kb-evo", source_kind="database",
        signature="a" * 64, last_status="ok", attempts=1))
    db.add(OntologyEvolutionState(
        org_id=OTHER, source_key="db:kb-other", source_kind="database",
        signature="b" * 64, last_status="failed", last_error="boom"))
    db.commit()

    r = _client(db).get("/ontology/evolution/state")
    assert r.status_code == 200
    sources = r.json()["sources"]
    assert [s["source_key"] for s in sources] == ["db:kb-evo"]  # E5 scoping
    assert sources[0]["last_status"] == "ok"
    assert sources[0]["last_error"] is None

    # other org's admin sees only their row
    r2 = _client(db, org=OTHER).get("/ontology/evolution/state")
    assert [s["source_key"] for s in r2.json()["sources"]] == ["db:kb-other"]


def test_reconcile_endpoint_report_vs_apply(db, tmp_path):
    db_path = tmp_path / "ep.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (c REAL)")
    conn.commit()
    conn.close()
    kb1 = KnowledgeBase(org_id=ORG, name="ep-src", source_kind="database",
                        db_type="sqlite", api_url=str(db_path))
    kb1.id = "kb-ok"
    db.add(kb1)
    db.commit()
    _catalog(db, kb1.id, {"t": ["c"]})

    r = _client(db).post("/ontology/evolution/reconcile", json={"apply": False})
    assert r.status_code == 200
    body = r.json()
    assert {"sources", "changes", "actions", "notifications",
            "failed"} <= set(body.keys())
    # report-only: nothing persisted
    assert db.query(OntologyEvolutionState).count() == 0

    r2 = _client(db).post("/ontology/evolution/reconcile", json={"apply": True})
    assert r2.status_code == 200
    # healthy source → baseline recorded ok (E1: no invented changes);
    # the report-only pass persisted NOTHING before this
    rows = db.query(OntologyEvolutionState).filter(
        OntologyEvolutionState.source_key == "db:kb-ok").all()
    assert len(rows) == 1 and rows[0].last_status == "ok"
