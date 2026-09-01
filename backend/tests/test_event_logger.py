"""Phase 5 — Usage instrumentation: event logging end-to-end.

Uses an isolated in-memory SQLite database (no production writes) and creates
only the ``artifact_events`` table, then verifies ``log_deck_event`` persists a
row and that unknown event types are rejected.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.artifact_event import ARTIFACT_EVENT_TYPES, ArtifactEvent
from app.services.artifacts.event_logger import log_deck_event


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    ArtifactEvent.__table__.create(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def test_log_deck_event_persists_row(db):
    log_deck_event(
        db, "deck_generated", artifact_id="art-1", user_id="u-1",
        metadata={"slide_count": 7, "has_preview": True},
        org_id="o-1", app_id="a-1",
    )
    rows = db.query(ArtifactEvent).all()
    assert len(rows) == 1
    ev = rows[0]
    assert ev.event_type == "deck_generated"
    assert ev.artifact_id == "art-1"
    assert ev.user_id == "u-1"
    assert "slide_count" in (ev.metadata_json or "")
    assert ev.org_id == "o-1"


def test_metadata_is_json_and_truncated(db):
    big = {"note": "x" * 5000}
    log_deck_event(db, "deck_edited", artifact_id="art-2", metadata=big)
    ev = db.query(ArtifactEvent).one()
    # Stored as JSON, and capped well under 5000 chars.
    assert ev.metadata_json.startswith("{")
    assert len(ev.metadata_json) < 5000


def test_unknown_event_type_rejected(db):
    log_deck_event(db, "deck_exploded", artifact_id="art-3")
    assert db.query(ArtifactEvent).count() == 0


def test_all_three_event_types_accepted(db):
    for et in ("deck_generated", "deck_edited", "deck_downloaded"):
        log_deck_event(db, et, artifact_id="art-x", metadata={"k": et})
    assert db.query(ArtifactEvent).count() == len(ARTIFACT_EVENT_TYPES)
