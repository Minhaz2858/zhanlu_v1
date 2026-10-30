"""C2 — append-only identity decision history.

Every identity decision (system creation, human confirm, human reject,
and reversals) is recorded as an append-only event preserving: previous
decision, new decision, reviewer identity, matching method (tier),
confidence, evidence, source record identifiers and timestamp. History
must be auditable and reconstructable per match, org-scoped."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import IdentityDecisionEvent, IdentityMatch

ORG = "org-h"
OTHER = "org-z"

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


def _resolve_one(db, object_id="obj-h"):
    from app.services.identity.resolver import _record_match

    m = _record_match(
        db, ORG, object_id, SRC_A, SRC_B, "1001", "C1001", "exact",
        confidence=1.0,
        evidence={"method": "exact_id", "normalized_value": "1001"},
    )
    db.commit()
    return m


def test_creation_records_append_only_event(db):
    from app.services.identity.review import decision_history

    m = _resolve_one(db)
    events = decision_history(db, ORG, m.id)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_kind"] == "created"
    assert ev["previous_status"] is None
    assert ev["new_status"] == "inferred"
    assert ev["reviewed_by"] is None            # system creation: no human
    assert ev["match_tier"] == "exact"          # matching method preserved
    assert ev["confidence"] == 1.0
    assert ev["evidence"]["method"] == "exact_id"
    assert ev["source_a_key"].startswith("file:")
    assert ev["pk_a"] == "1001" and ev["pk_b"] == "C1001"
    assert ev["timestamp"]


def test_confirm_and_reject_append_events(db):
    from app.services.identity.review import decision_history, review_match

    m = _resolve_one(db)
    review_match(db, ORG, m.id, "confirm", reviewed_by="boss")
    review_match(db, ORG, m.id, "reject", reviewed_by="chief")
    events = decision_history(db, ORG, m.id)
    assert [e["event_kind"] for e in events] == \
        ["created", "confirm", "reject"]
    confirm_ev = events[1]
    assert confirm_ev["previous_status"] == "inferred"
    assert confirm_ev["new_status"] == "confirmed"
    assert confirm_ev["reviewed_by"] == "boss"
    reject_ev = events[2]
    assert reject_ev["previous_status"] == "confirmed"   # reversal recorded
    assert reject_ev["new_status"] == "rejected"
    assert reject_ev["reviewed_by"] == "chief"


def test_reversal_rejected_back_to_confirmed(db):
    from app.services.identity.review import decision_history, review_match

    m = _resolve_one(db)
    review_match(db, ORG, m.id, "reject", reviewed_by="boss")
    review_match(db, ORG, m.id, "confirm", reviewed_by="chief2")
    events = decision_history(db, ORG, m.id)
    assert events[-1]["previous_status"] == "rejected"
    assert events[-1]["new_status"] == "confirmed"
    assert events[-1]["reviewed_by"] == "chief2"


def test_same_decision_rereview_is_rejected_no_event(db):
    from app.services.identity.review import decision_history, review_match

    m = _resolve_one(db)
    review_match(db, ORG, m.id, "confirm", reviewed_by="boss")
    with pytest.raises(ValueError):
        review_match(db, ORG, m.id, "confirm", reviewed_by="boss")
    assert len(decision_history(db, ORG, m.id)) == 2  # created + confirm


def test_history_org_scoped_and_unknown_match(db):
    from app.services.identity.review import decision_history

    m = _resolve_one(db)
    with pytest.raises(LookupError):
        decision_history(db, OTHER, m.id)
    with pytest.raises(LookupError):
        decision_history(db, ORG, "nope")


def test_no_unaudited_decision_path_survives(db):
    """The unaudited mark_review shortcut must be gone: every mutation of
    IdentityMatch.status goes through the audited review path."""
    import inspect

    import app.services.identity.resolver as resolver

    assert not hasattr(resolver, "mark_review"), (
        "unaudited mark_review must not exist — decisions only via "
        "review_match (audited + history)"
    )
