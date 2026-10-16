"""P3 final batch tests — quote drafts (P3-12), invoice matching
(P3-9), sentiment auto-escalation (P3-6b)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base

ORG = "org-fin"


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


# ── P3-12: quote drafts ──────────────────────────────────────────────────

def test_quote_direct_call_refused(db):
    import app.services.tool_handlers.quote_invoice_tools as qit

    out = asyncio.run(qit._quote_direct_call_guard(
        {"customer_name": "ACME"}, db, None, {"org_id": ORG}))
    assert not out["success"] and "governance approval" in out["reason"]


def test_quote_created_with_approval(db):
    import app.services.tool_handlers.quote_invoice_tools as qit
    from app.models.procurement import ProcurementPoDraft

    out = asyncio.run(qit._create_quote_draft(
        {"customer_name": "Contoso Ltd", "product": "Pilot license",
         "quantity": 10, "est_unit_price": 500},
        db, None, {"org_id": ORG, "approval_id": "appr-1"},
    ))
    assert out["success"] and out["kind"] == "quote"
    row = db.query(ProcurementPoDraft).filter(
        ProcurementPoDraft.id == out["draft_id"]).first()
    assert row.kind == "quote" and row.status == "approved"
    assert row.est_amount == 5000.0


def test_quote_registered():
    import app.services.tool_handlers  # noqa: F401
    from app.services.tool_registry import registry

    assert registry.get_entry("create_quote_draft") is not None
    assert registry.get_entry("match_invoice") is not None


# ── P3-9: invoice matching ───────────────────────────────────────────────

def _mk_bound(db, name):
    from app.models.ontology import OntologyObject

    obj = OntologyObject(
        id=f"o-{name}", org_id=ORG, domain_id="d1", name=name,
        backing_kind="table", backing_ref=f"kb:kb1:{name.lower()}",
        properties_json=[{"name": "amount", "type": "number"}],
    )
    db.add(obj)
    db.commit()
    return obj


def test_match_invoice_verdicts(db, monkeypatch):
    import app.services.tool_handlers.quote_invoice_tools as qit

    _mk_bound(db, "Invoice")
    _mk_bound(db, "PurchaseOrder")
    import app.services.ontology.data as data_mod

    rows = {"Invoice": [{"invoice_no": "I-1", "amount": 4250000}],
            "PurchaseOrder": [{"po_no": "PO-9", "amount": 4250000}]}
    monkeypatch.setattr(
        data_mod, "read_object_data",
        lambda db_, org_, obj, **kw: {"success": True, "rows": rows[obj.name]})
    import app.services.llm_service as llm
    monkeypatch.setattr(llm, "chat_completion_json_sync", lambda *a, **kw: {
        "matches": [{"invoice_index": 0, "match_index": 0,
                     "verdict": "matched", "confidence": 0.95,
                     "reason": "amounts equal"}]})

    out = asyncio.run(qit._match_invoice(
        {"invoice_object": "Invoice", "match_object": "PurchaseOrder"},
        db, None, {"org_id": ORG}))
    assert out["success"] and out["summary"]["matched"] == 1
    assert out["matches"][0]["verdict"] == "matched"


def test_match_invoice_unbound_refused(db):
    import app.services.tool_handlers.quote_invoice_tools as qit

    _mk_bound(db, "Invoice")
    from app.models.ontology import OntologyObject
    db.add(OntologyObject(id="o-po", org_id=ORG, domain_id="d1",
                          name="PurchaseOrder", backing_kind="manual"))
    db.commit()
    out = asyncio.run(qit._match_invoice(
        {"invoice_object": "Invoice", "match_object": "PurchaseOrder"},
        db, None, {"org_id": ORG}))
    assert not out["success"]


# ── P3-6b: sentiment nudge ───────────────────────────────────────────────

def _vec(base: list[float], scale: float = 1.0) -> list[float]:
    return [x * scale for x in base]


def test_nudge_fires_on_strong_frustration(monkeypatch):
    import app.services.escalation_sentiment as es

    # make exemplar embeddings deterministic: first exemplar = [1,0,0]
    monkeypatch.setattr(es, "_exemplar_embeddings",
                        lambda: [[1.0, 0.0, 0.0]] + [[0.0, 1.0, 0.0]] * 9)
    import app.services.memory_advanced.embeddings as emb
    monkeypatch.setattr(emb, "get_embedding",
                        lambda t, **kw: [0.95, 0.1, 0.0])  # near exemplar 0

    out = es.escalation_nudge_for_message("I want to speak to a human NOW")
    assert "USER FRUSTRATION SIGNAL" in out
    assert "escalate_to_human" in out


def test_nudge_silent_on_calm_message(monkeypatch):
    import app.services.escalation_sentiment as es

    monkeypatch.setattr(es, "_exemplar_embeddings",
                        lambda: [[1.0, 0.0, 0.0]])
    import app.services.memory_advanced.embeddings as emb
    monkeypatch.setattr(emb, "get_embedding",
                        lambda t, **kw: [0.0, 0.0, 1.0])  # orthogonal
    assert es.escalation_nudge_for_message("what is our Q3 revenue?") == ""


def test_nudge_fails_open(monkeypatch):
    import app.services.escalation_sentiment as es

    monkeypatch.setattr(es, "_exemplar_embeddings",
                        lambda: (_ for _ in ()).throw(RuntimeError("no model")))
    assert es.escalation_nudge_for_message("furious") == ""
    assert es.negative_sentiment_score(None) == 0.0
