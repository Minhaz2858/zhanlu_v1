"""
Test that ArtifactService.link_to_message is idempotent.

Calling link_to_message twice with the same (artifact_id, message_id)
must NOT create a duplicate message_artifacts row.  The second call
should return the existing link unchanged.
"""

import pytest
from uuid import uuid4
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal, engine, Base
from app.models.artifact import Artifact, MessageArtifact
from app.services.artifacts.artifact_service import ArtifactService


@pytest.fixture(autouse=True)
def setup_db():
    """Create all tables before each test, drop after."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    """SQLAlchemy session for direct DB operations."""
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _create_artifact(db, atype="md", title="Test Report", conv_id=None):
    """Create an artifact directly in DB so FK constraints are satisfied."""
    aid = str(uuid4())
    art = Artifact(
        id=aid,
        artifact_type=atype,
        title=title,
        status="draft",
        conversation_id=conv_id,
    )
    db.add(art)
    db.commit()
    return art


def test_link_to_message_idempotent_same_pair(db):
    """Calling link_to_message twice with the same (artifact_id, message_id)
    must result in exactly ONE message_artifacts row, and both calls must
    return a link with the same id."""

    mid = str(uuid4())
    cid = str(uuid4())
    art = _create_artifact(db, "md", "Report A", cid)
    svc = ArtifactService(db)

    link1 = svc.link_to_message(
        artifact_id=art.id,
        message_id=mid,
        conversation_id=cid,
        display_order=0,
    )
    link2 = svc.link_to_message(
        artifact_id=art.id,
        message_id=mid,
        conversation_id=cid,
        display_order=0,
    )

    # Both calls return the same link
    assert link1.id == link2.id

    # Exactly one row in the DB
    rows = db.query(MessageArtifact).filter(
        MessageArtifact.artifact_id == art.id,
        MessageArtifact.message_id == mid,
    ).all()
    assert len(rows) == 1
    assert rows[0].id == link1.id


def test_link_to_message_idempotent_preserves_original_display_order(db):
    """When a duplicate call comes in with a different display_order,
    the existing row is returned as-is (display_order is NOT updated)."""

    mid = str(uuid4())
    cid = str(uuid4())
    art = _create_artifact(db, "md", "Report B", cid)
    svc = ArtifactService(db)

    link1 = svc.link_to_message(
        artifact_id=art.id,
        message_id=mid,
        conversation_id=cid,
        display_order=0,
    )
    link2 = svc.link_to_message(
        artifact_id=art.id,
        message_id=mid,
        conversation_id=cid,
        display_order=5,  # different display_order — should be ignored
    )

    assert link1.id == link2.id
    assert link2.display_order == 0  # original value preserved

    rows = db.query(MessageArtifact).filter(
        MessageArtifact.artifact_id == art.id,
        MessageArtifact.message_id == mid,
    ).all()
    assert len(rows) == 1


def test_link_to_message_different_artifacts_same_message(db):
    """Different artifacts linked to the same message still create
    separate rows (idempotency is per (artifact_id, message_id) pair)."""

    mid = str(uuid4())
    cid = str(uuid4())
    art1 = _create_artifact(db, "md", "Report C1", cid)
    art2 = _create_artifact(db, "pptx", "Report C2", cid)
    svc = ArtifactService(db)

    link1 = svc.link_to_message(
        artifact_id=art1.id,
        message_id=mid,
        conversation_id=cid,
        display_order=0,
    )
    link2 = svc.link_to_message(
        artifact_id=art2.id,
        message_id=mid,
        conversation_id=cid,
        display_order=1,
    )

    assert link1.id != link2.id

    rows = db.query(MessageArtifact).filter(
        MessageArtifact.message_id == mid,
    ).all()
    assert len(rows) == 2
