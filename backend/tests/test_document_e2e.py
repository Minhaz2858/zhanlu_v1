"""P4 T10 — end-to-end P4 scenarios with REAL documents on disk.

Real .txt/.docx/.xlsx files are created and parsed by the REAL
extractors (extract_segments) and REAL chunker (chunk_segments); only
the LLM candidate step is stubbed — and its quotes are taken VERBATIM
from the real extracted chunk text (what a real extraction would
produce), so the deterministic quote verifier runs against genuinely
extracted content.

Covers the binding scenario set: quote verification, missing evidence
(fabricated quote → dropped), conflicting policies (verified definition
never overwritten, corroborating entry appends), document update → new
immutable version + supersede with history preserved, cross-org
extraction refused, review → revision history + decision events.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import DocumentVersion, KnowledgeBase
from app.models.ontology import (
    DocumentDecisionEvent,
    DocumentProposal,
    OntologyDomain,
    OntologyObject,
)
import app.models.ontology_revision  # noqa: F401 — register in metadata
from app.models.ontology_revision import OntologyRevision

ORG = "org-e2e"
OTHER = "org-other"

TXT_BODY = (
    "Customer means any entity that buys goods from the company.\n"
    "Refunds require approval by the finance department.\n"
    "The warranty covers defects for 12 months.\n"
)

SENTINEL_DEF = ("Customer means any entity that buys goods from the "
                "company.")
SENTINEL_RULE = "Refunds require approval by the finance department."


def _mk_domain(db) -> OntologyDomain:
    d = OntologyDomain(org_id=ORG, code="e2e", name="E2E")
    db.add(d)
    db.commit()
    return d


def _mk_kb(db, tmp_path, *, name="handbook", file_type="txt",
           org=ORG, body=None, project_id=None) -> KnowledgeBase:
    body = body if body is not None else TXT_BODY
    fname = f"{name}.{file_type}"
    (tmp_path / fname).write_text(body, encoding="utf-8")
    kb = KnowledgeBase(org_id=org, name=name, source_kind="file",
                       file_type=file_type, file_url=f"/api/uploads/{fname}",
                       project_id=project_id)
    db.add(kb)
    db.commit()
    return kb


def _mk_docx(tmp_path, *, name="playbook", body: dict[str, list[str]]) -> str:
    """Real DOCX via python-docx: {heading: [paragraphs]}."""
    from docx import Document

    doc = Document()
    for heading, paras in body.items():
        doc.add_heading(heading, level=1)
        for p in paras:
            doc.add_paragraph(p)
    path = tmp_path / f"{name}.docx"
    doc.save(str(path))
    return str(path)


def _mk_xlsx(tmp_path, *, name="pricebook", sheets: dict[str, list[list]]) -> str:
    """Real XLSX via openpyxl: {sheet_name: rows}."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for sheet, rows in sheets.items():
        ws = wb.create_sheet(title=sheet)
        for r in rows:
            ws.append(r)
    path = tmp_path / f"{name}.xlsx"
    wb.save(str(path))
    return str(path)


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
def flag_on(monkeypatch, tmp_path):
    import app.config as config
    import app.services.document_ingestion.service as svc

    monkeypatch.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED",
                        True, raising=False)
    # Hermetic upload root: the pipeline must read OUR files, never the
    # shared disk state.
    monkeypatch.setattr(svc, "_upload_root", lambda: tmp_path, raising=False)


@pytest.fixture()
def stub_llm(monkeypatch):
    """LLM stub whose quotes are VERBATIM spans of the real chunk text —
    it scans each real chunk for known sentinels, like a real extraction
    would. A deliberately fabricated quote is emitted for the chunk that
    contains 'Warranty' to exercise the deterministic verifier."""
    import app.services.ontology.document_grounding as dg

    def _stub(chunks):
        out = []
        for c in chunks:
            text = c["text"]
            if SENTINEL_DEF in text:
                out.append({
                    "quote": SENTINEL_DEF, "kind": "definition",
                    "payload": {"name": "Customer",
                                "description": "Buys goods from us."},
                    "_chunk_key": c["chunk_key"],
                })
            if SENTINEL_RULE in text:
                out.append({
                    "quote": SENTINEL_RULE, "kind": "approval_policy",
                    "payload": {"name": "Refund approval",
                                "description": "Finance approves."},
                    "_chunk_key": c["chunk_key"],
                })
            if "warranty covers" in text:
                out.append({
                    "quote": "The warranty was signed by the king of Spain.",
                    "kind": "rule", "payload": {"name": "Warranty"},
                    "_chunk_key": c["chunk_key"],
                })
        return out

    monkeypatch.setattr(dg, "_llm_propose", _stub)
    return _stub


def _extract(db, kb):
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    return extract_document_evidence(db, kb, org_id=kb.org_id)


def _approve(db, p, domain):
    from app.services.ontology.document_review import review_proposal

    return review_proposal(db, ORG, p.id, "approve", reviewed_by="admin-1",
                           domain_id=domain.id)


# ── real TXT: full pipeline → approval → revision history ────────────────

def test_txt_pipeline_approval_and_revision_history(db, tmp_path, flag_on,
                                                    stub_llm):
    domain = _mk_domain(db)
    kb = _mk_kb(db, tmp_path)
    report = _extract(db, kb)

    assert report["proposed"] == 2
    # The fabricated warranty quote FAILS deterministic verification —
    # counted separately, never proposed.
    assert report["verified_quote"] == 2
    assert report["dropped_untraceable"] == 1

    defn = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "definition").one()
    result = _approve(db, defn, domain)

    obj = db.query(OntologyObject).filter(
        OntologyObject.org_id == ORG, OntologyObject.name == "Customer").one()
    assert result["object_id"] == obj.id
    assert obj.evidence_status == "verified" and obj.confidence is None
    entry = obj.provenance_json["document_provenance"][0]
    assert entry["quote"] == SENTINEL_DEF
    assert entry["reviewer"] == "admin-1"

    # Revision history (P2) + decision events (append-only) exist.
    assert db.query(OntologyRevision).filter(
        OntologyRevision.entity_id == obj.id).count() == 1
    assert db.query(DocumentDecisionEvent).filter(
        DocumentDecisionEvent.proposal_id == defn.id).count() == 1


# ── real DOCX: section locations flow through to proposals ───────────────

def test_docx_real_file_carries_section_locations(db, tmp_path, flag_on,
                                                  stub_llm):
    _mk_domain(db)
    path = _mk_docx(tmp_path, body={
        "Definitions": [SENTINEL_DEF],
        "Refunds": [SENTINEL_RULE],
    })
    # The REAL extractor must expose the headings as sections.
    from app.services.document_ingestion import extractors

    segments = extractors.extract_segments(path, "docx")
    assert any(s["location"].get("section") == "Definitions"
               for s in segments)

    kb = KnowledgeBase(org_id=ORG, name="playbook", source_kind="file",
                       file_type="docx", file_url="/api/uploads/playbook.docx")
    db.add(kb)
    db.commit()

    report = _extract(db, kb)  # real extract → real chunking → stub LLM
    assert report["proposed"] == 2
    defn = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "definition").one()
    assert defn.location_json.get("section") == "Definitions"
    rule = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "approval_policy").one()
    assert rule.location_json.get("section") == "Refunds"


# ── real XLSX: sheet locations ────────────────────────────────────────────

def test_xlsx_real_file_carries_sheet_locations(db, tmp_path, flag_on,
                                                stub_llm):
    _mk_domain(db)
    path = _mk_xlsx(tmp_path, sheets={
        "Pricing": [["item", "price"], ["widget", 100]],
        "Policy": [[SENTINEL_RULE]],
    })
    from app.services.document_ingestion import extractors

    segments = extractors.extract_segments(path, "xlsx")
    assert any(s["location"].get("sheet") == "Policy" for s in segments)

    kb = KnowledgeBase(org_id=ORG, name="pricebook", source_kind="file",
                       file_type="xlsx", file_url="/api/uploads/pricebook.xlsx")
    db.add(kb)
    db.commit()

    report = _extract(db, kb)
    assert report["proposed"] == 1
    rule = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "approval_policy").one()
    assert rule.location_json.get("sheet") == "Policy"


# ── document update → new immutable version, supersede, history kept ─────

def test_document_update_supersedes_and_preserves_history(db, tmp_path,
                                                          flag_on, stub_llm):
    domain = _mk_domain(db)
    kb = _mk_kb(db, tmp_path)
    _extract(db, kb)
    defn = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "definition").one()
    rule = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "approval_policy").one()
    _approve(db, defn, domain)
    from app.services.ontology.document_review import review_proposal

    review_proposal(db, ORG, rule.id, "reject", reviewed_by="admin-1")
    old_version_id = kb.content_version_id
    obj = db.query(OntologyObject).filter(
        OntologyObject.name == "Customer").one()

    # Re-ingest with CHANGED content (sentinel removed, new text added).
    (tmp_path / "handbook.txt").write_text(
        "Refunds require approval by the finance department.\n"
        "All invoices are due within 30 days.\n", encoding="utf-8")
    report2 = _extract(db, kb)

    assert kb.content_version_id != old_version_id  # new immutable version
    assert db.query(DocumentVersion).filter(
        DocumentVersion.kb_id == kb.id).count() == 2
    # Old proposals: `proposed` ones are superseded (never deleted); the
    # APPROVED one keeps its status — the human decision stands and the
    # approved object is not invalidated by a document edit. Decision
    # events survive either way.
    db.expire_all()
    assert db.query(DocumentProposal).filter(
        DocumentProposal.id == defn.id).one().status == "approved"
    assert db.query(DocumentProposal).filter(
        DocumentProposal.id == rule.id).one().status == "rejected"
    assert db.query(DocumentDecisionEvent).filter(
        DocumentDecisionEvent.proposal_id == defn.id).count() == 1
    # The approved object is untouched by superseding.
    db.refresh(obj)
    assert obj.evidence_status == "verified"
    # The rejected rule was NOT re-proposed from the unchanged remainder.
    names = [p.payload_json.get("name") for p in
             db.query(DocumentProposal).filter(
                 DocumentProposal.status == "proposed").all()]
    assert "Refund approval" not in names


# ── conflicting policies across sources: verified never overwritten ──────

def test_conflicting_sources_verified_definition_preserved(db, tmp_path,
                                                           flag_on,
                                                           stub_llm):
    domain = _mk_domain(db)
    kb1 = _mk_kb(db, tmp_path, name="handbook")
    _extract(db, kb1)
    defn = db.query(DocumentProposal).filter(
        DocumentProposal.proposal_kind == "definition").one()
    _approve(db, defn, domain)
    obj = db.query(OntologyObject).filter(
        OntologyObject.name == "Customer").one()
    original_description = obj.description

    # A SECOND source proposes the same element.
    kb2 = _mk_kb(db, tmp_path, name="partner-handbook")
    _extract(db, kb2)
    defn2 = db.query(DocumentProposal).filter(
        DocumentProposal.kb_id == kb2.id,
        DocumentProposal.proposal_kind == "definition").one()
    assert defn2.conflict_json  # extraction flagged it for a human

    result = _approve(db, defn2, domain)
    db.expire_all()
    db.refresh(obj)
    # Same object; the definition was NOT overwritten; the corroborating
    # document appended its own entry (per-entry D7).
    assert result["object_id"] == obj.id
    assert obj.description == original_description
    entries = obj.provenance_json["document_provenance"]
    assert len(entries) == 2
    assert entries[0]["source_kb_id"] != entries[1]["source_kb_id"]


# ── cross-org extraction refused ──────────────────────────────────────────

def test_cross_org_extraction_refused(db, tmp_path, flag_on, stub_llm):
    from fastapi import HTTPException

    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    kb = _mk_kb(db, tmp_path, org=OTHER)
    with pytest.raises(HTTPException) as exc:
        extract_document_evidence(db, kb, org_id=ORG)
    assert exc.value.status_code == 403
