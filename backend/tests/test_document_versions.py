"""P4 T1 — document versioning: immutable, file-level content identity.

Binding scenarios (plan v3):
1. Re-ingesting UNCHANGED content creates NO new version.
2. Changed content creates a NEW immutable version.
Version rows are never updated or deleted; the current pointer lives on
knowledge_bases (content_hash + content_version_id).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import DocumentVersion, KnowledgeBase

ORG = "org-docver"


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
def file_kb(db, tmp_path):
    """A file KB whose backing file exists on disk (upload-root patched)."""
    import app.services.document_ingestion.service as svc

    def _make(name: str, content: str) -> KnowledgeBase:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        kb = KnowledgeBase(
            org_id=ORG, name=name, source_kind="file",
            file_type="txt", file_url=f"/api/uploads/{name}",
        )
        db.add(kb)
        db.commit()
        return kb

    svc._upload_root = lambda: tmp_path
    return _make


def _record_version(db, kb):
    from app.services.document_ingestion.versioning import record_document_version

    return record_document_version(db, kb)


def test_unchanged_reingest_creates_no_new_version(db, file_kb):
    """Scenario 1: unchanged content → NO new version row."""
    kb = file_kb("doc.txt", "stable content")

    v1 = _record_version(db, kb)
    assert v1 is not None
    assert kb.content_hash == v1.content_hash
    assert kb.content_version_id == v1.id

    # Re-record with the SAME bytes (simulated unchanged re-ingest).
    v2 = _record_version(db, kb)
    assert v2 is None, "unchanged content must not create a new version"

    rows = db.query(DocumentVersion).filter_by(kb_id=kb.id).all()
    assert len(rows) == 1
    # The pointer still references the original immutable row.
    assert kb.content_version_id == v1.id


def test_changed_content_creates_new_immutable_version(db, file_kb):
    """Scenario 2: changed content → NEW immutable version; old rows never
    updated or deleted."""
    kb = file_kb("doc.txt", "version one")

    v1 = _record_version(db, kb)
    first_hash = v1.content_hash
    first_seen = v1.first_indexed_at

    # Content changes on disk (same KB, same file_url).
    import app.services.document_ingestion.service as svc
    (svc._upload_root() / "doc.txt").write_text("version two", encoding="utf-8")

    v2 = _record_version(db, kb)
    assert v2 is not None
    assert v2.id != v1.id
    assert v2.content_hash != first_hash

    # Pointer moved to the new version.
    db.refresh(kb)
    assert kb.content_hash == v2.content_hash
    assert kb.content_version_id == v2.id

    # History: BOTH rows exist, the old one untouched (immutability).
    rows = (
        db.query(DocumentVersion)
        .filter_by(kb_id=kb.id)
        .order_by(DocumentVersion.first_indexed_at)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].content_hash == first_hash
    assert rows[0].first_indexed_at == first_seen
    assert rows[1].content_hash == v2.content_hash


def test_hash_computed_from_actual_file_bytes(db, file_kb):
    """SHA-256 is over the real bytes, not the file name or metadata."""
    import hashlib

    kb = file_kb("doc.txt", "exact bytes matter")
    v = _record_version(db, kb)
    expected = hashlib.sha256(b"exact bytes matter").hexdigest()
    assert v.content_hash == expected
    assert v.content_bytes == len(b"exact bytes matter")


def test_missing_file_records_no_version(db, file_kb, tmp_path):
    """A vanished file cannot fabricate a version (honest failure)."""
    kb = file_kb("ghost.txt", "will disappear")
    (tmp_path / "ghost.txt").unlink()
    assert _record_version(db, kb) is None
    assert kb.content_hash is None
