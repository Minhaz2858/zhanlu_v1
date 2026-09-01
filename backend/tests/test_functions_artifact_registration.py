"""Tests for artifact registration inside the legacy doc-generation endpoints.

The chat assistant invokes ``POST /api/apps/{app_id}/functions/generatePptx``
and ``POST /api/apps/{app_id}/functions/generateReportDocx`` whenever the LLM
emits a ``create_resource`` tool call. Historically these endpoints only wrote
a file to ``/api/uploads/{uuid}.{ext}`` and returned a ``file_url`` — they did
**not** create a row in the ``artifacts`` table. As a result the chat UI's
inline preview (``GET /api/artifacts/{artifactId}``) 404'd and the side panel
rendered ``"Failed to load artifact: HTTP 404"``.

These tests pin down the new contract: the endpoints must register the
generated file as a governed Artifact, return an ``artifact_id`` and a
``preview_url``, and the artifact must be retrievable through the standard
``ArtifactService.get_artifact()`` path.
"""

# IMPORTANT: env vars must be set BEFORE importing the app.
import os
import sys
import tempfile
import atexit

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

# Per-module file-backed SQLite so that TestClient and the test's own db
# session see the same data (in-memory SQLite would create a new DB per
# connection, hiding TestClient's writes from the test's reads).
_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE.name}"
os.environ["MD_ARTIFACT_ENABLED"] = "true"
os.environ["ARTIFACT_STORAGE_BACKEND"] = "postgres_bytea"

import pytest
from fastapi.testclient import TestClient

from main import app  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402

# Create all tables once for the module
Base.metadata.create_all(bind=engine)

APP_ID = "local-zhanlu-app"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _cleanup_db():
    """Best-effort cleanup; the tempfile is removed by the OS anyway."""
    try:
        Base.metadata.drop_all(bind=engine)
    except Exception:
        pass
    try:
        os.unlink(_DB_FILE.name)
    except Exception:
        pass


atexit.register(_cleanup_db)


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def test_generate_pptx_returns_artifact_id_and_preview_url(client):
    """``generatePptx`` should now return ``artifact_id`` and ``preview_url``."""
    resp = client.post(
        f"/api/apps/{APP_ID}/functions/generatePptx",
        json={
            "title": "Quarterly Review",
            "slides": [
                {"title": "Highlights", "bullets": ["Revenue +12%", "NPS 64"]},
                {"title": "Next Steps", "bullets": ["Launch Q3", "Hire 2 SEs"]},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Old contract preserved
    assert "file_url" in body and body["file_url"].endswith(".pptx")
    assert body.get("status") == "ready"
    assert body.get("title") == "Quarterly Review"
    # New contract
    assert body.get("artifact_id"), f"Missing artifact_id in response: {body}"
    assert body.get("preview_url"), f"Missing preview_url in response: {body}"


def test_generate_pptx_artifact_is_retrievable(client, db):
    """The artifact returned in the response should exist in the DB and be fetchable."""
    resp = client.post(
        f"/api/apps/{APP_ID}/functions/generatePptx",
        json={
            "title": "Lookup Me",
            "slides": [{"title": "A", "bullets": ["a1"]}],
        },
    )
    assert resp.status_code == 200, resp.text
    artifact_id = resp.json()["artifact_id"]
    assert artifact_id

    from app.services.artifacts.artifact_service import ArtifactService
    art = ArtifactService(db).get_artifact(artifact_id)
    assert art is not None
    assert art.title == "Lookup Me"
    # The artifact should have at least one version and at least an original blob
    assert len(art.versions) >= 1
    blobs = art.versions[0].blobs
    assert any(b.blob_type == "original" for b in blobs)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def test_generate_report_docx_returns_artifact_id_and_preview_url(client):
    """``generateReportDocx`` should also return ``artifact_id`` and ``preview_url``."""
    resp = client.post(
        f"/api/apps/{APP_ID}/functions/generateReportDocx",
        json={
            "title": "Weekly Status Report",
            "markdown": "## Highlights\n- Revenue up 8%",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["file_url"].endswith(".docx")
    assert body.get("artifact_id"), f"Missing artifact_id in response: {body}"
    assert body.get("preview_url"), f"Missing preview_url in response: {body}"


def test_generate_report_docx_artifact_is_retrievable(client, db):
    resp = client.post(
        f"/api/apps/{APP_ID}/functions/generateReportDocx",
        json={
            "title": "DOCX Lookup",
            "markdown": "## Highlights\n- Revenue up 8%",
        },
    )
    assert resp.status_code == 200
    artifact_id = resp.json()["artifact_id"]

    from app.services.artifacts.artifact_service import ArtifactService
    art = ArtifactService(db).get_artifact(artifact_id)
    assert art is not None
    assert art.title == "DOCX Lookup"
    blobs = art.versions[0].blobs
    assert any(b.blob_type == "original" for b in blobs)


# ---------------------------------------------------------------------------
# Inline preview endpoint must succeed (no 404) after registration
# ---------------------------------------------------------------------------


def test_inline_artifact_endpoint_succeeds_after_generation(client):
    """Once the artifact is registered, the inline endpoint must return 200."""
    gen = client.post(
        f"/api/apps/{APP_ID}/functions/generatePptx",
        json={
            "title": "Inline Test",
            "slides": [{"title": "A", "bullets": ["x"]}],
        },
    )
    assert gen.status_code == 200, gen.text
    artifact_id = gen.json()["artifact_id"]

    # The inline endpoint is GET /api/artifacts/{id}
    fetch = client.get(f"/api/artifacts/{artifact_id}")
    assert fetch.status_code == 200, (
        f"Inline preview returned {fetch.status_code} — the 404 bug is back. "
        f"Body: {fetch.text[:200]}"
    )


def test_inline_artifact_endpoint_succeeds_after_docx_generation(client):
    """Same guarantee for DOCX."""
    gen = client.post(
        f"/api/apps/{APP_ID}/functions/generateReportDocx",
        json={
            "title": "Inline DOCX",
            "markdown": "## Highlights\n- A bullet",
        },
    )
    assert gen.status_code == 200, gen.text
    artifact_id = gen.json()["artifact_id"]

    fetch = client.get(f"/api/artifacts/{artifact_id}")
    assert fetch.status_code == 200, (
        f"Inline DOCX preview returned {fetch.status_code} — the 404 bug is back. "
        f"Body: {fetch.text[:200]}"
    )
