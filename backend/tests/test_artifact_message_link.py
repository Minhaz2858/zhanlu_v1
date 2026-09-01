"""
Test the MessageArtifact link contract used by inline preview.

Exercises:
  POST /api/artifacts/{artifact_id}/link
  GET  /api/messages/{message_id}/artifacts

These endpoints are consumed by ArtifactPreviewCardList in the frontend
to render inline file cards. This test locks in the data shape.
"""

import pytest
from fastapi.testclient import TestClient
from uuid import uuid4
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import app
from app.database import SessionLocal, engine, Base
from app.models.artifact import Artifact, MessageArtifact
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.agent_conversation import AgentConversation


@pytest.fixture(autouse=True)
def setup_db():
    """Create all tables before each test, drop after."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    """FastAPI TestClient."""
    with TestClient(app) as c:
        yield c


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


def _create_test_data(db):
    """Create ChatSession + ChatMessage."""
    sid = str(uuid4())
    mid = str(uuid4())
    cid = str(uuid4())

    db.add(ChatSession(id=sid, title="Test", project="test"))
    db.add(ChatMessage(id=mid, session_id=sid, role="assistant",
                       content="Here is the report.", order=1))
    db.add(AgentConversation(id=cid, title="TestConv", agent_name="TestAgent"))
    db.commit()
    return sid, mid, cid


# -- Tests --

def test_link_artifact_to_message_and_fetch(client, db):
    """TDD RED: Link an artifact to a message, then fetch it back."""
    sid, mid, cid = _create_test_data(db)
    art = _create_artifact(db, "md", "Q2 Sales Report", cid)

    # Link via API
    resp = client.post(f"/api/artifacts/{art.id}/link", json={
        "message_id": mid,
        "conversation_id": cid,
        "display_order": 0,
    })
    assert resp.status_code == 200, f"Link failed: {resp.text}"
    link = resp.json()
    assert link["message_id"] == mid
    assert link["artifact_id"] == art.id

    # Fetch via API
    resp2 = client.get(f"/api/messages/{mid}/artifacts")
    assert resp2.status_code == 200
    data = resp2.json()
    assert len(data) == 1
    a = data[0]
    assert a["artifact_id"] == art.id
    assert a["artifact_type"] == "md"
    assert a["title"] == "Q2 Sales Report"

    for field in ["artifact_id", "artifact_type", "title", "status"]:
        assert field in a, f"Missing field '{field}'"


def test_get_message_artifacts_empty(client, db):
    sid, mid, cid = _create_test_data(db)
    resp = client.get(f"/api/messages/{mid}/artifacts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_link_multiple_artifacts(client, db):
    sid, mid, cid = _create_test_data(db)

    ids = []
    for i, atype in enumerate(["md", "xlsx", "html"]):
        art = _create_artifact(db, atype, f"Report {i}", cid)
        ids.append(art.id)
        resp = client.post(f"/api/artifacts/{art.id}/link", json={
            "message_id": mid, "conversation_id": cid, "display_order": i,
        })
        assert resp.status_code == 200

    data = client.get(f"/api/messages/{mid}/artifacts").json()
    assert len(data) == 3
    assert [a["artifact_id"] for a in data] == ids


def test_link_nonexistent_artifact_no_500(client, db):
    resp = client.post(f"/api/artifacts/{uuid4()}/link", json={
        "message_id": str(uuid4()),
        "conversation_id": str(uuid4()),
        "display_order": 0,
    })
    assert resp.status_code == 404, (
        f"Expected 404 for nonexistent artifact, got {resp.status_code}"
    )
    assert "not found" in resp.json()["detail"].lower()
