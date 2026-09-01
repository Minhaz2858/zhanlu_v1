"""
Test the BlobStorage abstraction with Postgres backend round-trip.

Exercises:
  - PostgresBlobStorage put/get/delete/exists
  - ArtifactService.store_blob uses storage backend
  - ArtifactService.get_blob_data resolves through storage_uri
"""

import pytest
from fastapi.testclient import TestClient
from uuid import uuid4
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import app
from app.database import SessionLocal, engine, Base
from app.models.artifact import Artifact, ArtifactVersion, ArtifactBlob
from app.services.artifacts.storage import PostgresBlobStorage, get_blob_storage


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


def _create_artifact_and_version(db):
    """Create a minimal artifact + version so blob storage can be tested."""
    aid = str(uuid4())
    vid = str(uuid4())
    art = Artifact(id=aid, artifact_type="html", title="Storage Test", status="draft")
    ver = ArtifactVersion(id=vid, artifact_id=aid, version_number=1, status="building")
    db.add(art)
    db.add(ver)
    db.commit()
    return aid, vid


def _create_blob_row(db, version_id):
    """Create a blob row with data=None, storage_uri set."""
    import hashlib

    bid = str(uuid4())
    blob = ArtifactBlob(
        id=bid,
        version_id=version_id,
        blob_type="original",
        file_name="test.html",
        mime_type="text/html",
        file_size=0,
        checksum=hashlib.sha256(b"").hexdigest(),
        data=None,
        storage_uri=f"inline://{bid}",
    )
    db.add(blob)
    db.commit()
    return bid, blob


# -- PostgresBlobStorage round-trip --


def test_put_and_get_round_trip(db):
    """Put bytes, get them back, verify content."""
    aid, vid = _create_artifact_and_version(db)
    bid, blob = _create_blob_row(db, vid)

    storage = PostgresBlobStorage(db)
    test_data = b"<html><body><h1>Hello</h1></body></html>"

    # Put
    storage.put(blob.storage_uri, test_data)

    # Get
    result = storage.get(blob.storage_uri)
    assert result == test_data


def test_get_nonexistent_uri(db):
    """Getting a nonexistent URI returns None."""
    storage = PostgresBlobStorage(db)
    result = storage.get("inline://nonexistent-id")
    assert result is None


def test_exists(db):
    """exists() returns True after put, False for missing data."""
    aid, vid = _create_artifact_and_version(db)
    bid, blob = _create_blob_row(db, vid)

    storage = PostgresBlobStorage(db)

    # Before put
    assert storage.exists(blob.storage_uri) is False

    # After put
    storage.put(blob.storage_uri, b"data")
    assert storage.exists(blob.storage_uri) is True


def test_delete(db):
    """delete() removes data, makes exists() return False."""
    aid, vid = _create_artifact_and_version(db)
    bid, blob = _create_blob_row(db, vid)

    storage = PostgresBlobStorage(db)
    storage.put(blob.storage_uri, b"some data")

    assert storage.delete(blob.storage_uri) is True
    assert storage.exists(blob.storage_uri) is False
    assert storage.get(blob.storage_uri) is None

    # Deleting already-deleted returns False
    assert storage.delete(blob.storage_uri) is False


# -- ArtifactService store_blob uses storage backend --


def test_store_blob_sets_storage_uri(db):
    """After store_blob, the blob has a storage_uri and data can be retrieved."""
    from app.services.artifacts.artifact_service import ArtifactService

    aid, vid = _create_artifact_and_version(db)
    svc = ArtifactService(db)

    html_data = b"<html><body>Artifact test</body></html>"
    blob = svc.store_blob(
        version_id=vid,
        blob_type="original",
        file_name="report.html",
        mime_type="text/html; charset=utf-8",
        data=html_data,
    )

    assert blob.storage_uri is not None
    assert blob.storage_uri.startswith("inline://")
    assert blob.checksum is not None

    # Verify data retrievable via get_blob_data
    retrieved = svc.get_blob_data(blob)
    assert retrieved == html_data


def test_get_blob_data_backward_compat(db):
    """get_blob_data returns data column directly if populated (backward compat)."""
    aid, vid = _create_artifact_and_version(db)
    bid = str(uuid4())

    legacy = ArtifactBlob(
        id=bid,
        version_id=vid,
        blob_type="original",
        file_name="legacy.html",
        mime_type="text/html",
        file_size=11,
        checksum="abc123",
        data=b"legacy data",  # directly in data column
        storage_uri=None,
    )
    db.add(legacy)
    db.commit()

    from app.services.artifacts.artifact_service import ArtifactService
    svc = ArtifactService(db)
    result = svc.get_blob_data(legacy)
    assert result == b"legacy data"


# -- Factory --


def test_get_blob_storage_factory_returns_postgres(db):
    """get_blob_storage with a db_session returns PostgresBlobStorage."""
    storage = get_blob_storage(db)
    assert isinstance(storage, PostgresBlobStorage)
