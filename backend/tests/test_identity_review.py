"""T5 — identity review queue: listing, org-admin confirm/reject with
`identity_review` audit events, and honest errors."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import IdentityMatch

ORG = "org-review"
OTHER = "org-other"

SRC_A = {"kind": "file", "kb_id": "kb-a", "sheet": "CRM"}
SRC_B = {"kind": "file", "kb_id": "kb-b", "sheet": "Master"}


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
def seeded(db):
    from app.services.identity.resolver import _record_match

    _record_match(db, ORG, "obj-1", SRC_A, SRC_B, "1001", "C1001",
                  "semantic", status="inferred", confidence=0.91)
    _record_match(db, ORG, "obj-1", SRC_A, SRC_B, "1002", "C1002",
                  "probabilistic", status="inferred", confidence=0.7)
    _record_match(db, OTHER, "obj-1", SRC_A, SRC_B, "1001", "C1001",
                  "semantic", status="inferred", confidence=0.9)
    db.commit()
    return db


def test_list_is_org_scoped_and_filters(seeded):
    from app.services.identity.review import list_matches

    rows = list_matches(seeded, ORG)
    assert len(rows) == 2                      # other-org row invisible
    inferred = list_matches(seeded, ORG, status="inferred")
    assert len(inferred) == 2
    by_pk = {r["pk_a"]: r for r in inferred}
    assert by_pk["1001"]["match_tier"] == "semantic"
    assert by_pk["1001"]["confidence"] == 0.91
    assert by_pk["1001"]["source_a"]["kb_id"] == "kb-a"


def test_confirm_records_audit_and_status(seeded):
    from app.models.audit_event import AuditEvent
    from app.services.identity.review import list_matches, review_match

    m_id = list_matches(seeded, ORG, status="inferred")[0]["id"]
    out = review_match(seeded, ORG, m_id, "confirm", reviewed_by="boss")
    assert out["status"] == "confirmed"
    assert out["reviewed_by"] == "boss"

    audits = seeded.query(AuditEvent).filter(
        AuditEvent.event_type == "identity_review").all()
    assert len(audits) == 1
    assert audits[0].org_id == ORG
    assert audits[0].detail_json["decision"] == "confirm"
    assert audits[0].detail_json["match_id"] == m_id
    assert audits[0].detail_json["match_tier"] in ("semantic", "probabilistic")


def test_reject_records_decision(seeded):
    from app.services.identity.review import list_matches, review_match

    rows = list_matches(seeded, ORG, status="inferred")
    out = review_match(seeded, ORG, rows[0]["id"], "reject",
                       reviewed_by="boss")
    assert out["status"] == "rejected"


def test_review_unknown_or_bad_decision_errors(seeded):
    from app.services.identity.review import review_match

    with pytest.raises(LookupError):
        review_match(seeded, ORG, "nope", "confirm", reviewed_by="boss")

    m_id = seeded.query(IdentityMatch).filter_by(org_id=ORG).first().id
    with pytest.raises(ValueError):
        review_match(seeded, ORG, m_id, "maybe", reviewed_by="boss")


def test_review_cannot_touch_other_org(seeded):
    from app.services.identity.review import review_match

    other_id = seeded.query(IdentityMatch).filter_by(org_id=OTHER).first().id
    with pytest.raises(LookupError):
        review_match(seeded, ORG, other_id, "confirm", reviewed_by="boss")
