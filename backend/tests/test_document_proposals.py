"""P4 T4 — ontology_document_proposals: model, org isolation, idempotency,
document identity anchoring (doc_version_id)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import DocumentProposal

ORG = "org-p"


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


def _proposal(org=ORG, kb="kb-1", ver="ver-1", kind="definition",
              quote="Customer means any entity that buys goods."):
    return DocumentProposal(
        org_id=org, kb_id=kb, doc_version_id=ver,
        source_kb_id=kb,
        chunk_key=f"{kb}_{ver}_c7",
        proposal_kind=kind,
        payload_json={"name": "Customer",
                      "description": "An entity that buys goods."},
        quote=quote,
        location_json={"page": 3},
        method="llm_extraction",
        model_version="deepseek-chat-2026-10",
    )


def test_create_and_query_scoped_to_org(db):
    db.add(_proposal())
    db.add(_proposal(org="org-other"))
    db.commit()
    mine = db.query(DocumentProposal).filter_by(org_id=ORG).all()
    assert len(mine) == 1
    assert mine[0].status == "proposed"  # default — never auto-verified
    assert mine[0].reviewed_by is None   # system evidence, no human yet


def test_idempotency_constraint_blocks_duplicate_proposal(db):
    db.add(_proposal())
    db.commit()
    db.add(_proposal())  # same org/kb/version/chunk/kind/quote
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_quote_is_original_verbatim_span(db):
    """The stored quote is the ORIGINAL span (un-normalized) — whitespace
    normalization is a verifier concern only."""
    raw = "Customer   means\nany entity  that buys goods."
    db.add(_proposal(quote=raw))
    db.commit()
    row = db.query(DocumentProposal).first()
    assert row.quote == raw  # exactly as extracted


def test_evidence_counts_columns(db):
    """Per-run extraction counters live on the pipeline report, but the
    proposal row must know its provenance: doc_version + chunk + method +
    model version + timestamp."""
    p = _proposal()
    db.add(p)
    db.commit()
    assert p.doc_version_id == "ver-1"
    assert p.chunk_key
    assert p.method == "llm_extraction"
    assert p.model_version
    assert p.created_date is not None
