"""Tests for document ingestion — extractors, chunker, embedder (mocked), store, service.

All tests mock the embedder via monkeypatch so the real sentence-transformers
model is never loaded. The store tests use a temp Chroma dir; the service
test uses an in-memory SQLite engine so the real Postgres DB is untouched.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import TimestampedBase
from app.services.document_ingestion import embedder as embedder_mod


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

def test_extract_text_txt(tmp_path):
    from app.services.document_ingestion.extractors import extract_text
    p = tmp_path / "note.txt"
    p.write_text("hello world\nsecond line", encoding="utf-8")
    out = extract_text(str(p), file_type="txt")
    assert "hello world" in out
    assert "second line" in out


def test_extract_text_md(tmp_path):
    from app.services.document_ingestion.extractors import extract_text
    p = tmp_path / "doc.md"
    p.write_text("# Title\n\nSome **bold** text.", encoding="utf-8")
    out = extract_text(str(p), file_type="md")
    assert "Title" in out
    assert "bold" in out
    assert "#" not in out  # markdown markup stripped


def test_extract_text_csv(tmp_path):
    from app.services.document_ingestion.extractors import extract_text
    p = tmp_path / "data.csv"
    p.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")
    out = extract_text(str(p), file_type="csv")
    assert "name" in out and "age" in out
    assert "Alice" in out and "30" in out


def test_extract_text_unknown_type_returns_empty(tmp_path):
    from app.services.document_ingestion.extractors import extract_text
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01")
    out = extract_text(str(p), file_type="bin")
    assert out == ""


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

def test_chunk_text_short_returns_one_chunk():
    from app.services.document_ingestion.chunker import chunk_text
    out = chunk_text("hello world", max_tokens=800, overlap=100)
    assert len(out) == 1
    assert "hello world" in out[0]["text"]


def test_chunk_text_splits_long_text():
    from app.services.document_ingestion.chunker import chunk_text
    text = "alpha beta gamma delta epsilon zeta eta theta. " * 200
    out = chunk_text(text, max_tokens=800, overlap=100)
    assert len(out) > 1
    for c in out:
        assert "text" in c and "index" in c


def test_chunk_text_respects_paragraph_boundary():
    from app.services.document_ingestion.chunker import chunk_text
    text = "para one.\n\n" + ("word " * 1000) + "\n\npara three."
    out = chunk_text(text, max_tokens=200, overlap=20)
    assert len(out) >= 2


# ---------------------------------------------------------------------------
# Embedder (mocked)
# ---------------------------------------------------------------------------

def test_embedder_singleton(monkeypatch):
    import numpy as np
    calls = {"n": 0}

    class FakeModel:
        def encode(self, texts, **kw):
            calls["n"] += 1
            return np.array([[0.1, 0.2] for _ in texts])

    monkeypatch.setattr(embedder_mod, "_load_model", lambda: FakeModel())
    embedder_mod.reset_for_tests()
    e1 = embedder_mod.get_embedder()
    e2 = embedder_mod.get_embedder()
    assert e1 is e2  # singleton
    vecs = embedder_mod.embed_texts(["a", "b"])
    assert vecs.shape == (2, 2)
    assert calls["n"] == 1  # model loaded once
    embedder_mod.reset_for_tests()


# ---------------------------------------------------------------------------
# Store (mocked embedder + temp chroma dir)
# ---------------------------------------------------------------------------

def test_store_upsert_and_query(tmp_path, monkeypatch):
    import numpy as np
    from app.services.document_ingestion import store as store_mod

    class FakeModel:
        def encode(self, texts, **kw):
            return np.array([[1.0, 0.0] if "cat" in t else [0.0, 1.0] for t in texts])

    monkeypatch.setattr(embedder_mod, "_load_model", lambda: FakeModel())
    embedder_mod.reset_for_tests()
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    store_mod.reset_for_tests()

    store_mod.upsert_chunks(
        org_id="org-1",
        kb_id="kb-1",
        chunks=[
            {"text": "the cat sat", "index": 0},
            {"text": "the dog ran", "index": 1},
        ],
        metas=[
            {"file_name": "a.txt", "file_type": "txt"},
            {"file_name": "a.txt", "file_type": "txt"},
        ],
    )
    res = store_mod.query(org_id="org-1", kb_ids=["kb-1"], query_text="cat", top_k=1)
    assert res["chunks"]
    assert "cat" in res["chunks"][0]["text"]

    store_mod.delete_kb(org_id="org-1", kb_id="kb-1")
    res2 = store_mod.query(org_id="org-1", kb_ids=["kb-1"], query_text="cat", top_k=1)
    assert not res2["chunks"]

    embedder_mod.reset_for_tests()
    store_mod.reset_for_tests()


# ---------------------------------------------------------------------------
# Service / orchestrator (in-memory SQLite + mocked embedder)
# ---------------------------------------------------------------------------

def test_ingest_kb_end_to_end(tmp_path, monkeypatch):
    import numpy as np
    from app.services.document_ingestion import service as svc, store as store_mod
    from app.models.knowledge_base import KnowledgeBase

    class FakeModel:
        def encode(self, texts, **kw):
            return np.array([[1.0, 0.0] for _ in texts])

    monkeypatch.setattr(embedder_mod, "_load_model", lambda: FakeModel())
    embedder_mod.reset_for_tests()
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    store_mod.reset_for_tests()

    # in-memory SQLite so we don't touch the real Postgres
    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # write an upload file
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "note.txt").write_text(
        "hello world from doc\n\nsecond paragraph here", encoding="utf-8"
    )
    monkeypatch.setattr(svc, "_upload_root", lambda: upload_dir)

    db = Session()
    try:
        kb = KnowledgeBase(
            name="doc",
            source_kind="file",
            file_type="txt",
            file_url="/api/uploads/note.txt",
            org_id="org-x",
            app_id="app-x",
        )
        db.add(kb)
        db.commit()
        db.refresh(kb)

        ok = svc.ingest_kb(db, kb.id)
        assert ok is True
        db.refresh(kb)
        assert kb.indexing_status == "ready"
        assert kb.chunk_count and kb.chunk_count >= 1
        assert kb.last_indexed_at is not None

        status = svc.get_status(db, kb.id)
        assert status["found"] is True
        assert status["indexing_status"] == "ready"
    finally:
        db.close()
        embedder_mod.reset_for_tests()
        store_mod.reset_for_tests()


def test_ingest_kb_missing_file_sets_failed(tmp_path, monkeypatch):
    import numpy as np
    from app.services.document_ingestion import service as svc, store as store_mod
    from app.models.knowledge_base import KnowledgeBase

    class FakeModel:
        def encode(self, texts, **kw):
            return np.array([[1.0, 0.0] for _ in texts])

    monkeypatch.setattr(embedder_mod, "_load_model", lambda: FakeModel())
    embedder_mod.reset_for_tests()
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    store_mod.reset_for_tests()
    monkeypatch.setattr(svc, "_upload_root", lambda: tmp_path / "uploads")

    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        kb = KnowledgeBase(
            name="missing",
            source_kind="file",
            file_type="txt",
            file_url="/api/uploads/nonexistent.txt",
            org_id="org-y",
            app_id="app-y",
        )
        db.add(kb)
        db.commit()
        db.refresh(kb)

        ok = svc.ingest_kb(db, kb.id)
        assert ok is False
        db.refresh(kb)
        assert kb.indexing_status == "failed"
        assert kb.index_error
    finally:
        db.close()
        embedder_mod.reset_for_tests()
        store_mod.reset_for_tests()
