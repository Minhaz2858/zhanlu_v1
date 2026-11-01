"""Phase 3D.1 — prov-v1 adoption at the document + process evidence emitters.

``_to_dict`` (document review serialization) and ``get_process`` (EBPM
process detail) now emit the canonical provenance contract alongside ALL
legacy fields. Additive only: legacy keys unchanged, statuses pass
through verbatim (proposed stays proposed), missing confidence stays
missing, retrieval never implies answer support, review history
untouched, org isolation preserved (get_process stays fail-closed).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.enterprise_process import (
    BusinessProcess, ProcessActivity, ProcessEvidence, ProcessLink)
from app.models.ontology import DocumentProposal
from app.services.enterprise.provenance import (
    PROVENANCE_VERSION,
    from_document_proposal,
    serialize_provenance,
)
from app.services.ontology.document_review import _to_dict
from app.services.ontology.enterprise_discovery import get_process

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        DocumentProposal.__table__, BusinessProcess.__table__,
        ProcessActivity.__table__, ProcessEvidence.__table__,
        ProcessLink.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _proposal(db, pid, *, org=ORG, status="proposed", confidence=None):
    db.add(DocumentProposal(
        id=pid, org_id=org, kb_id="kb-1", doc_version_id="dv-1",
        source_kb_id="kb-1", chunk_key=f"{pid}-chunk-3",
        proposal_kind="entity", quote=f"verbatim quote of {pid}",
        method="llm_propose_v1", status=status))
    db.commit()


def _process(db, pid, *, org=ORG, with_evidence=True):
    db.add(BusinessProcess(
        id=pid, org_id=org, name="Order to Cash",
        discovery_status="documented", evidence_status="verified",
        confidence=0.9,
        provenance_json={"review_history": [
            {"decision": "confirmed", "reviewer_id": "u-1", "reason": "ok",
             "at": "2026-11-01T00:00:00+00:00"}]}))
    if with_evidence:
        db.add(ProcessEvidence(
            id=f"{pid}-ev1", org_id=org, process_id=pid, activity_seq=1,
            evidence_kind="document_chunk",
            source_ref="doc://kb-1/po-terms", quote="Payment within 30 days",
            confidence=0.92, evidence_status="verified"))
        db.add(ProcessEvidence(
            id=f"{pid}-ev2", org_id=org, process_id=pid, activity_seq=2,
            evidence_kind="execution_log", source_ref="log://run-7",
            quote=None, confidence=None, evidence_status="proposed"))
    db.commit()


# 1-2. Document evidence conversion + serialization --------------------------------

def test_document_to_dict_emits_canonical_provenance(db):
    _proposal(db, "dp-1")
    p = db.get(DocumentProposal, "dp-1")
    d = _to_dict(p)
    cp = d["canonical_provenance"]
    assert cp["source_type"] == "document"
    assert cp["evidence_id"] == "dp-1"
    assert cp["source_ref"] == "dp-1-chunk-3"
    assert cp["org_id"] == ORG
    assert cp["retrieved"] is True and cp["influenced"] is False
    # the entry validates under the contract and fits the envelope
    assert serialize_provenance([cp])["version"] == PROVENANCE_VERSION


def test_document_status_passes_through_verbatim(db):
    for status in ("proposed", "approved", "rejected", "superseded"):
        _proposal(db, f"dp-{status}", status=status)
        p = db.get(DocumentProposal, f"dp-{status}")
        assert _to_dict(p)["canonical_provenance"]["evidence_status"] == status


# 3. Process evidence conversion + serialization ------------------------------------

def test_process_detail_emits_canonical_provenance(db):
    _process(db, "proc-1")
    detail = get_process(db, ORG, "proc-1")
    assert detail is not None
    ev = {e["id"]: e for e in detail["evidence"]}
    cp1 = ev["proc-1-ev1"]["canonical_provenance"]
    assert cp1["source_type"] == "process_evidence"
    assert cp1["process_id"] == "proc-1"
    assert cp1["source_ref"] == "doc://kb-1/po-terms"
    assert cp1["evidence_status"] == "verified"
    assert cp1["confidence"] == 0.92  # passthrough, never invented
    assert cp1["retrieved"] is True and cp1["influenced"] is False
    assert "supports_answer" not in cp1  # retrieval never implies support
    cp2 = ev["proc-1-ev2"]["canonical_provenance"]
    assert cp2["evidence_status"] == "proposed"  # never promoted
    assert "confidence" not in cp2  # missing confidence stays missing
    assert "quote" not in cp2  # contract carries refs; quote stays legacy


# 4. Legacy API response compatibility -----------------------------------------------

LEGACY_DOC_KEYS = {
    "id", "org_id", "kb_id", "doc_version_id", "source_kb_id", "chunk_key",
    "proposal_kind", "payload_json", "quote", "location_json", "method",
    "model_version", "audience_json", "status", "object_id", "conflict_json",
    "reviewed_by", "reviewed_at", "created_date",
}


def test_document_legacy_keys_unchanged(db):
    _proposal(db, "dp-1")
    d = _to_dict(db.get(DocumentProposal, "dp-1"))
    assert LEGACY_DOC_KEYS <= set(d)  # every legacy field still present
    assert d["quote"] == "verbatim quote of dp-1"  # verbatim quote preserved
    assert d["chunk_key"] == "dp-1-chunk-3"


def test_process_legacy_keys_and_review_history_unchanged(db):
    _process(db, "proc-1")
    detail = get_process(db, ORG, "proc-1")
    for key in ("id", "name", "evidence_status", "confidence", "provenance",
                "review_history", "activities", "evidence",
                "related_processes", "ontology_connections"):
        assert key in detail
    # pre-existing review history intact; verification status untouched
    assert detail["review_history"][0]["decision"] == "confirmed"
    assert detail["evidence_status"] == "verified"
    # legacy evidence item fields preserved alongside the new key
    ev = detail["evidence"][0]
    for key in ("id", "evidence_kind", "source_ref", "quote", "confidence",
                "evidence_status"):
        assert key in ev


# 5. Cross-org isolation ----------------------------------------------------------------

def test_process_detail_cross_org_fail_closed(db):
    _process(db, "proc-1")
    assert get_process(db, "org-other", "proc-1") is None


def test_document_converter_stamps_caller_org(db):
    _proposal(db, "dp-x", org="org-other")
    p = db.get(DocumentProposal, "dp-x")
    cp = from_document_proposal(p)
    assert cp["org_id"] == "org-other"  # verbatim stamp, no cross-org fetch
    assert cp["source_ref"] == "dp-x-chunk-3"
