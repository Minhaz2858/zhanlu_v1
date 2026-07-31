"""
Test that _collect_artifact_results treats run_sandbox_skill results
identically to create_artifact results (Task 1.3: one canonical render
path, swappable engines).

The sandbox tool emits the same canonical keys (artifact_id, version_id,
file_url, preview_url, title, type, file_name, mime_type, file_size,
has_preview) so the collector picks it up and links it to the message
exactly like an LLM-driven create_artifact call.
"""

import pytest
from uuid import uuid4
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal, engine, Base
from app.models.artifact import Artifact, MessageArtifact
from app.routers.agents import _collect_artifact_results


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _sandbox_tool_call(artifact_id: str) -> dict:
    """A run_sandbox_skill tool_call record as the turn pipeline builds it."""
    return {
        "id": f"call-{artifact_id}",
        "name": "run_sandbox_skill",
        "status": "completed",
        "results": {
            "success": True,
            "artifact_id": artifact_id,
            "version_id": str(uuid4()),
            "version_number": 1,
            "file_url": f"/api/artifacts/{artifact_id}/download",
            "preview_url": f"/api/artifacts/{artifact_id}/preview",
            "title": "Sandbox Deck",
            "type": "pptx",
            "file_name": "report.pptx",
            "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "file_size": 12345,
            "has_preview": True,
        },
    }


def test_collect_accepts_run_sandbox_skill(db):
    """A successful run_sandbox_skill record is collected into
    message.artifacts with the canonical entry shape."""
    mid, cid = str(uuid4()), str(uuid4())
    art = Artifact(
        id=str(uuid4()), artifact_type="pptx", title="Sandbox Deck",
        status="preview_ready", conversation_id=cid,
    )
    db.add(art)
    db.commit()

    entries = _collect_artifact_results(
        [_sandbox_tool_call(art.id)], mid, cid, db,
    )

    assert len(entries) == 1
    entry = entries[0]
    for key in (
        "artifact_id", "version_id", "version_number", "file_url",
        "preview_url", "title", "type", "file_name", "mime_type",
        "file_size", "has_preview",
    ):
        assert key in entry, f"missing canonical key: {key}"
    assert entry["artifact_id"] == art.id
    assert entry["type"] == "pptx"

    # Linked to the message exactly like the create_artifact path
    links = db.query(MessageArtifact).filter(
        MessageArtifact.message_id == mid,
        MessageArtifact.artifact_id == art.id,
    ).all()
    assert len(links) == 1


def test_collect_skips_failed_sandbox_result(db):
    """A failed run_sandbox_skill result produces no artifact entry."""
    mid, cid = str(uuid4()), str(uuid4())
    tc = _sandbox_tool_call(str(uuid4()))
    tc["status"] = "failed"
    tc["results"] = {"success": False, "error": "sandbox timeout"}

    entries = _collect_artifact_results([tc], mid, cid, db)
    assert entries == []


def test_collect_mixed_create_artifact_and_sandbox(db):
    """Both engines' results coexist in one message.artifacts list."""
    mid, cid = str(uuid4()), str(uuid4())
    a1 = Artifact(id=str(uuid4()), artifact_type="docx", title="LLM Doc",
                  status="preview_ready", conversation_id=cid)
    a2 = Artifact(id=str(uuid4()), artifact_type="pptx", title="Sandbox Deck",
                  status="preview_ready", conversation_id=cid)
    db.add_all([a1, a2])
    db.commit()

    create_tc = {
        "id": "call-1", "name": "create_artifact", "status": "completed",
        "results": {
            "success": True, "artifact_id": a1.id,
            "version_id": str(uuid4()), "version_number": 1,
            "file_url": f"/api/artifacts/{a1.id}/download",
            "preview_url": f"/api/artifacts/{a1.id}/preview",
            "title": "LLM Doc", "type": "docx", "file_name": "doc.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": 999, "has_preview": True,
        },
    }

    entries = _collect_artifact_results(
        [create_tc, _sandbox_tool_call(a2.id)], mid, cid, db,
    )

    assert {e["artifact_id"] for e in entries} == {a1.id, a2.id}
    links = db.query(MessageArtifact).filter(
        MessageArtifact.message_id == mid,
    ).all()
    assert len(links) == 2
