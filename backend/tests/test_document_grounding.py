"""P4 T6 — document-grounding extraction + deduplication pipeline.

Flag-gated; LLM candidate step is stubbed in tests (untrusted document
content: the pipeline treats document text as DATA); deterministic quote
verification gates every candidate; idempotent per (kb, doc_version,
chunk, kind, quote); superseding preserves history.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import DocumentProposal, OntologyObject

ORG = "org-g"
OTHER = "org-h"

DOC_V1 = (
    "Policy handbook.\n"
    "Customer means any entity that buys goods from the company.\n"
    "Refunds require approval by the finance department.\n"
)


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
def grounded_kb(db, tmp_path, monkeypatch):
    """A file KB with a real document on disk and the pipeline flag ON."""
    import app.config as config
    import app.services.document_ingestion.service as svc

    monkeypatch.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED",
                        True, raising=False)
    # Isolate the upload root so tests never read shared disk state.
    monkeypatch.setattr(svc, "_upload_root", lambda: tmp_path, raising=False)
    (tmp_path / "handbook.txt").write_text(DOC_V1, encoding="utf-8")
    kb = KnowledgeBase(org_id=ORG, name="handbook", source_kind="file",
                       file_type="txt", file_url="/api/uploads/handbook.txt")
    db.add(kb)
    db.commit()
    return kb


@pytest.fixture()
def stub_llm(monkeypatch):
    """Stub the LLM candidate step: returns fixed candidates."""
    import app.services.ontology.document_grounding as dg

    def _stub(chunks):
        out = []
        for c in chunks:
            if "Customer means" in c["text"]:
                out.append({
                    "quote": "Customer means any entity that buys goods "
                             "from the company.",
                    "kind": "definition",
                    "payload": {"name": "Customer",
                                "description": "An entity that buys goods."},
                    "_chunk_key": c["chunk_key"],
                })
            if "Refunds require approval" in c["text"]:
                out.append({
                    "quote": "Refunds require approval by the finance "
                             "department.",
                    "kind": "approval_policy",
                    "payload": {"name": "Refund approval",
                                "description": "Finance approves refunds."},
                    "_chunk_key": c["chunk_key"],
                })
        return out

    monkeypatch.setattr(dg, "_llm_propose", _stub)
    return _stub


def test_flag_off_is_noop(db, grounded_kb, stub_llm):
    import app.config as config
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    monkeypatch_fail = config.settings
    monkeypatch_fail.ONTOLOGY_DOC_GROUNDING_ENABLED = False
    report = extract_document_evidence(db, grounded_kb)
    assert report["skipped"] is True
    assert db.query(DocumentProposal).count() == 0


def test_happy_path_creates_proposals_with_full_provenance(
        db, grounded_kb, stub_llm):
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    report = extract_document_evidence(db, grounded_kb)
    assert report["proposed"] == 2
    assert report["dropped_untraceable"] == 0
    assert report["duplicates_skipped"] == 0

    rows = db.query(DocumentProposal).filter_by(org_id=ORG).all()
    assert len(rows) == 2
    for r in rows:
        assert r.status == "proposed"
        assert r.reviewed_by is None
        assert r.doc_version_id == grounded_kb.content_version_id
        assert r.chunk_key
        assert r.method
        assert r.model_version
        assert r.location_json == {} or isinstance(r.location_json, dict)
        assert r.audience_json and r.audience_json.get("org_id") == ORG
    kinds = {r.proposal_kind for r in rows}
    assert kinds == {"definition", "approval_policy"}
    # quote stored verbatim as it appears in the source
    cust = next(r for r in rows if r.proposal_kind == "definition")
    assert "Customer means" in DOC_V1
    assert cust.quote.startswith("Customer means")


def test_untraceable_quote_dropped_and_counted(db, grounded_kb, monkeypatch):
    import app.services.ontology.document_grounding as dg
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    def _bad(chunks):
        first_key = chunks[0]["chunk_key"] if chunks else ""
        refunds_key = next(
            (c["chunk_key"] for c in chunks
             if "Refunds require approval" in c["text"]), first_key)
        return [{"quote": "The moon is made of cheese",  # not in document
                 "kind": "definition",
                 "payload": {"name": "Moon", "description": "cheese"},
                 "_chunk_key": first_key},
                {"quote": "Refunds require approval by the finance "
                          "department.",
                 "kind": "approval_policy",
                 "payload": {"name": "Refund approval"},
                 "_chunk_key": refunds_key}]

    monkeypatch.setattr(dg, "_llm_propose", _bad)
    report = extract_document_evidence(db, grounded_kb)
    assert report["dropped_untraceable"] == 1
    assert report["proposed"] == 1
    assert db.query(DocumentProposal).filter_by(
        proposal_kind="definition").count() == 0


def test_rerun_is_idempotent(db, grounded_kb, stub_llm):
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    extract_document_evidence(db, grounded_kb)
    report2 = extract_document_evidence(db, grounded_kb)
    assert report2["proposed"] == 0
    assert report2["duplicates_skipped"] == 2
    assert db.query(DocumentProposal).count() == 2


def test_conflict_with_existing_object_flagged_not_autoresolved(
        db, grounded_kb, stub_llm):
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    db.add(OntologyObject(org_id=ORG, domain_id="d1", name="customer",
                          backing_kind="manual", backing_ref=""))
    db.commit()

    extract_document_evidence(db, grounded_kb)
    cust = db.query(DocumentProposal).filter_by(
        proposal_kind="definition").first()
    assert cust.status == "proposed"  # human resolves, never automatic
    assert cust.conflict_json and cust.conflict_json.get("existing_object_id")


def test_cross_org_kb_refused(db, grounded_kb, stub_llm, monkeypatch):
    from fastapi import HTTPException

    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    grounded_kb.org_id = OTHER
    db.commit()
    with pytest.raises(HTTPException) as ei:
        extract_document_evidence(db, grounded_kb, org_id=ORG)
    assert ei.value.status_code == 403
    grounded_kb.org_id = ORG
    db.commit()


def test_non_file_kb_refused(db, monkeypatch):
    import app.config as config
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    monkeypatch.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED",
                        True, raising=False)
    kb = KnowledgeBase(org_id=ORG, name="db-src", source_kind="database",
                       db_type="sqlite")
    db.add(kb)
    db.commit()
    report = extract_document_evidence(db, kb)
    assert report["skipped"] is True


def test_document_update_supersedes_old_proposals_keeps_history(
        db, grounded_kb, stub_llm, tmp_path, monkeypatch):
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )
    from app.services.document_ingestion.versioning import (
        record_document_version,
    )

    extract_document_evidence(db, grounded_kb)
    old = db.query(DocumentProposal).filter_by(org_id=ORG).all()
    old_ids = {r.id for r in old}
    old_version = grounded_kb.content_version_id

    # Document content changes → new immutable version → re-extract.
    import app.services.document_ingestion.service as svc
    (svc._upload_root() / "handbook.txt").write_text(
        DOC_V1 + "\nWarranty covers manufacturing defects for two years.\n",
        encoding="utf-8")
    record_document_version(db, grounded_kb)
    assert grounded_kb.content_version_id != old_version

    def _warranty(chunks):
        key = next(
            (c["chunk_key"] for c in chunks
             if "Warranty covers" in c["text"]),
            chunks[0]["chunk_key"] if chunks else "")
        return [{"quote": "Warranty covers manufacturing defects for two "
                          "years.",
                 "kind": "rule",
                 "payload": {"name": "Warranty",
                             "description": "Two-year warranty."},
                 "_chunk_key": key}]

    import app.services.ontology.document_grounding as dg
    monkeypatch.setattr(dg, "_llm_propose", _warranty)
    report = extract_document_evidence(db, grounded_kb)
    assert report["proposed"] == 1

    # Old proposals superseded but PRESERVED (scenario 3).
    for pid in old_ids:
        row = db.get(DocumentProposal, pid)
        assert row is not None, "history must be preserved"
        assert row.status == "superseded"
        assert row.doc_version_id == old_version
