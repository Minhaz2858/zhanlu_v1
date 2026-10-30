"""P4 final acceptance tests — the security & integration matrix.

Closes the sign-off questions from the acceptance review:

A1  A restricted document's quotation stays hidden from an org member
    WITHOUT source-document access — across the object serialization,
    the document-evidence list, AND the revision feed (whole-response
    JSON substring checks, not just field asserts).
A2  The APPROVED definition itself IS visible to that member (org-wide
    approval policy — explicit design interpretation, pinned here so it
    can never silently flip).
A3  Revision history never exposes the restricted quotation (member vs
    owner see different snapshots of the same revision).
A4  A principal with NO authorization context (external API key path)
    receives no document-derived content by default.
A5  Real PDF (reportlab-generated) extraction produces PAGE-LEVEL
    citations on the proposals.

Only the LLM candidate step is stubbed (quotes verbatim from real chunk
text); extractors, chunker, verifier, pipeline, review, gates and
serialization all run for real.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import (
    DocumentProposal,
    OntologyDomain,
    OntologyObject,
)
import app.models.ontology_revision  # noqa: F401 — register in metadata
from app.models.user import User

ORG = "org-accept"

SENTINEL_DEF = ("Customer means any entity that buys goods from the "
                "company.")
SENTINEL_RULE = "Refunds require approval by the finance department."
TXT_BODY = f"{SENTINEL_DEF}\n{SENTINEL_RULE}\n"


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
def env(db, tmp_path, monkeypatch):
    """Flag ON, hermetic upload root, restricted source document, two
    proposals extracted, and the definition APPROVED by an org-admin who
    holds source access (the only principal allowed to approve)."""
    import app.config as config
    import app.services.document_ingestion.service as svc

    monkeypatch.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED",
                        True, raising=False)
    monkeypatch.setattr(svc, "_upload_root", lambda: tmp_path, raising=False)

    import app.services.ontology.document_grounding as dg

    def _stub(chunks):
        out = []
        for c in chunks:
            if SENTINEL_DEF in c["text"]:
                out.append({
                    "quote": SENTINEL_DEF, "kind": "definition",
                    "payload": {"name": "Customer",
                                "description": "Buys goods from us."},
                    "_chunk_key": c["chunk_key"],
                })
            if SENTINEL_RULE in c["text"]:
                out.append({
                    "quote": SENTINEL_RULE, "kind": "approval_policy",
                    "payload": {"name": "Refund approval",
                                "description": "Finance approves."},
                    "_chunk_key": c["chunk_key"],
                })
        return out

    monkeypatch.setattr(dg, "_llm_propose", _stub)

    (tmp_path / "handbook.txt").write_text(TXT_BODY, encoding="utf-8")
    owner = User(org_id=ORG, role="admin", email="owner@x.io",
                 full_name="Owner", password_hash="x")
    db.add(owner)
    db.flush()
    kb = KnowledgeBase(org_id=ORG, name="handbook", source_kind="file",
                       file_type="txt", file_url="/api/uploads/handbook.txt",
                       project_id="proj-sec",  # RESTRICTED source
                       created_by_id=owner.id)  # owner = document creator
    db.add(kb)
    domain = OntologyDomain(org_id=ORG, code="acc", name="Acc")
    db.add(domain)
    db.commit()

    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )
    from app.services.ontology.document_review import review_proposal

    extract_document_evidence(db, kb, org_id=ORG)
    defn = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "definition").one()
    review_proposal(db, ORG, defn.id, "approve", reviewed_by="admin-owner",
                    domain_id=domain.id)
    return {"kb": kb, "domain": domain}


def _client_for(db, user):
    from app.deps import get_current_user_required, get_db
    from app.routers.ontology import router as ont_router

    app = FastAPI()
    app.include_router(ont_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    from fastapi.testclient import TestClient

    return TestClient(app)


def _member(db) -> User:
    u = User(org_id=ORG, role="user", email="member@x.io",
             full_name="Member", password_hash="x")
    db.add(u)
    db.commit()
    return u


def _owner(db) -> User:
    return db.query(User).filter(
        User.org_id == ORG, User.role == "admin").one()


def _approved_object(db) -> OntologyObject:
    return db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG,
        OntologyObject.name == "Customer").one()


# ── A1 + A2: member without source access sees the DEFINITION, never the
#    quotation — object view, evidence list, and raw JSON everywhere ──────

def test_a1_a2_member_sees_definition_never_quote(db, env):
    member = _member(db)
    c = _client_for(db, member)
    obj = _approved_object(db)

    # A2 — the org-wide policy: approved definition visible.
    r = c.get(f"/api/ontology/objects?domain_id={env['domain'].id}")
    assert r.status_code == 200
    payload = r.json()
    items = payload.get("objects") if isinstance(payload, dict) else payload
    body = next(o for o in items if o["id"] == obj.id)
    assert body["name"] == "Customer"
    assert body["description"] == "Buys goods from us."
    assert body["evidence_status"] == "verified"

    # A1 — no trace of the quotation anywhere in the response.
    raw = json.dumps(body)
    assert SENTINEL_DEF not in raw
    entries = (body.get("provenance_json") or {}).get(
        "document_provenance")
    assert entries in (None, [])

    # The review-queue LIST is admin-only — a plain member gets 403, so
    # the queue cannot leak to non-admins at all.
    r2 = c.get("/api/ontology/document-evidence")
    assert r2.status_code == 403

    # An ADMIN who still lacks source-document access sees the queue but
    # the restricted proposals are hidden with an honest hidden count.
    outsider_admin = User(org_id=ORG, role="admin", email="oa@x.io",
                          full_name="OA", password_hash="x")
    db.add(outsider_admin)
    db.commit()
    ca = _client_for(db, outsider_admin)
    r3 = ca.get("/api/ontology/document-evidence")
    assert r3.status_code == 200
    listing = r3.json()
    ids = [p["id"] for p in listing["proposals"]]
    restricted = db.query(DocumentProposal).filter(
        DocumentProposal.kb_id == env["kb"].id).all()
    for p in restricted:
        assert p.id not in ids
        assert p.quote not in json.dumps(listing)
    assert listing["hidden"] >= 1


# ── A3: revision history — member scrubbed, owner sees the entry ─────────

def test_a3_revision_history_member_scrubbed_owner_sees(db, env):
    member = _member(db)
    owner = _owner(db)
    obj = _approved_object(db)

    cm = _client_for(db, member)
    co = _client_for(db, owner)

    rm = cm.get(f"/api/ontology/object/{obj.id}/revisions")
    assert rm.status_code == 200
    raw_m = json.dumps(rm.json())
    assert SENTINEL_DEF not in raw_m  # the quote is gone for the member
    snap_m = rm.json()["revisions"][0]["after"]
    assert (snap_m.get("provenance_json") or {}).get(
        "document_provenance") in (None, [])

    # The principal WITH source access still sees the full entry —
    # scrubbing is per-viewer, not destructive.
    ro = co.get(f"/api/ontology/object/{obj.id}/revisions")
    assert ro.status_code == 200
    snap_o = ro.json()["revisions"][0]["after"]
    kept = (snap_o.get("provenance_json") or {}).get(
        "document_provenance")
    assert kept and kept[0]["quote"] == SENTINEL_DEF

    # ...and the stored revision row itself is untouched (append-only).
    db.expire_all()
    from app.models.ontology_revision import OntologyRevision

    row = db.query(OntologyRevision).filter(
        OntologyRevision.entity_id == obj.id).one()
    stored = json.loads(row.after_json) if isinstance(
        row.after_json, str) else row.after_json
    stored_entries = (stored.get("provenance_json") or {}).get(
        "document_provenance")
    assert stored_entries and stored_entries[0]["quote"] == SENTINEL_DEF


# ── A4: no authorization context → no document-derived content ───────────

def test_a4_no_auth_context_default_strips_document_content(db, env):
    from app.services.ontology.serialize import object_to_dict

    obj = _approved_object(db)
    d = object_to_dict(obj)  # external API-key path: NO doc_ctx passed
    raw = json.dumps(d)
    assert SENTINEL_DEF not in raw
    assert "document_provenance" not in raw
    assert (d.get("provenance_json") or {}).get(
        "document_provenance") in (None, [])


# ── A5: real PDF → page-level citations ───────────────────────────────────

def test_a5_real_pdf_page_level_citations(db, tmp_path, env, monkeypatch):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "policy.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    c.drawString(72, 720, SENTINEL_DEF)
    c.showPage()  # page 2
    c.drawString(72, 720, SENTINEL_RULE)
    c.showPage()
    c.save()

    # The REAL extractor must attribute the sentences to pages 1 and 2.
    from app.services.document_ingestion import extractors

    segments = extractors.extract_segments(str(pdf_path), "pdf")
    pages = {s["text"]: s["location"].get("page") for s in segments}
    assert pages.get(SENTINEL_DEF) == 1
    assert pages.get(SENTINEL_RULE) == 2

    import app.config as config

    monkeypatch.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED",
                        True, raising=False)
    kb = KnowledgeBase(org_id=ORG, name="policy", source_kind="file",
                       file_type="pdf", file_url="/api/uploads/policy.pdf")
    db.add(kb)
    db.commit()

    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    report = extract_document_evidence(db, kb, org_id=ORG)
    assert report["proposed"] == 2
    defn = db.query(DocumentProposal).filter(
        DocumentProposal.kb_id == kb.id,
        DocumentProposal.proposal_kind == "definition").one()
    assert defn.location_json.get("page") == 1
    rule = db.query(DocumentProposal).filter(
        DocumentProposal.kb_id == kb.id,
        DocumentProposal.proposal_kind == "approval_policy").one()
    assert rule.location_json.get("page") == 2
