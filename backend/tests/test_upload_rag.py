"""Tests for chat-upload RAG (document_ingestion.upload_rag).

Covers: index → retrieve round-trip, session isolation, idempotent
re-indexing, fail-open degradation, availability probe, and the prompt
block formatter. Uses a deterministic mock embedding function + a temp
Chroma dir — no model download, no network.
"""

from __future__ import annotations

import shutil

import pytest

from app.config import settings
from app.services.document_ingestion import upload_rag
from app.services.document_ingestion.store import CollectionStore, reset_for_tests


class MockEmbeddingFn:
    """Deterministic 16-dim char-hash embedding (mirrors test_store_upgrade)."""

    DIM = 16

    def name(self) -> str:
        return "mock-embedding-fn-v1"

    def __call__(self, input):
        return self.embed_documents(input)

    def embed_documents(self, texts):
        return [self._embed_one(t) for t in texts]

    def embed_query(self, input=None, text=None):
        q = input if input is not None else text
        if isinstance(q, list):
            q = q[0] if q else ""
        return [self._embed_one(str(q) if q else "")]

    @staticmethod
    def _embed_one(text):
        if not text:
            return [0.0] * 16
        vec = [0.0] * 16
        for i, ch in enumerate(text[:64]):
            vec[(ord(ch) + i) % 16] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


@pytest.fixture
def rag_env(monkeypatch, tmp_path):
    """Point CHROMA_DIR at a temp dir and bind upload_rag._store to it."""
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    monkeypatch.setenv("CHROMA_DIR", str(chroma_dir))
    reset_for_tests()
    upload_rag.reset_for_tests()
    monkeypatch.setattr(settings, "RAG_UPLOADS_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_UPLOADS_COLLECTION", "chat_uploads")
    monkeypatch.setattr(settings, "RAG_UPLOADS_TOP_K", 8)

    store_holder = {}

    def _fake_store(org_id, embedding_fn=None, client=None):
        key = f"{org_id}|{id(embedding_fn)}|{id(client)}"
        if key not in store_holder:
            store_holder[key] = CollectionStore(
                org_id=org_id,
                collection_name="chat_uploads",
                embedding_fn=MockEmbeddingFn(),
            )
        return store_holder[key]

    monkeypatch.setattr(upload_rag, "_store", _fake_store)
    yield store_holder
    upload_rag.reset_for_tests()
    shutil.rmtree(chroma_dir, ignore_errors=True)


LONG_TEXT = (
    "The quarterly contract review covers pricing for DCPD resin across "
    "the C5/C9 segments. " * 40
)


class TestIndexAndRetrieve:
    def test_roundtrip_returns_relevant_chunk(self, rag_env):
        n = upload_rag.index_upload_text(
            "/api/uploads/a.txt", "sess-1", "org-1", LONG_TEXT, file_name="a.txt"
        )
        assert n > 0
        chunks = upload_rag.retrieve_upload_chunks(
            "sess-1", "org-1", "DCPD resin pricing C5 C9"
        )
        assert chunks, "expected at least one retrieved chunk"
        assert any("DCPD" in c["text"] for c in chunks)
        assert all(c["metadata"]["session_id"] == "sess-1" for c in chunks)

    def test_session_isolation(self, rag_env):
        upload_rag.index_upload_text(
            "/api/uploads/a.txt", "sess-A", "org-1", LONG_TEXT, file_name="a.txt"
        )
        chunks_b = upload_rag.retrieve_upload_chunks(
            "sess-B", "org-1", "DCPD resin pricing"
        )
        assert chunks_b == []

    def test_reindex_is_idempotent(self, rag_env):
        n1 = upload_rag.index_upload_text(
            "/api/uploads/a.txt", "sess-1", "org-1", LONG_TEXT, file_name="a.txt"
        )
        n2 = upload_rag.index_upload_text(
            "/api/uploads/a.txt", "sess-1", "org-1", LONG_TEXT, file_name="a.txt"
        )
        assert n1 > 0
        assert n2 == n1
        chunks = upload_rag.retrieve_upload_chunks(
            "sess-1", "org-1", "DCPD resin"
        )
        assert len(chunks) <= n1

    def test_blank_text_indexes_zero(self, rag_env):
        assert upload_rag.index_upload_text("/api/uploads/a.txt", "s", "o", "   ") == 0


class TestFailOpen:
    def test_index_never_raises(self, rag_env, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("chroma down")

        monkeypatch.setattr(upload_rag, "_store", _boom)
        assert upload_rag.index_upload_text("/api/uploads/a.txt", "s", "o", "x" * 100) == 0

    def test_retrieve_never_raises(self, rag_env, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("chroma down")

        monkeypatch.setattr(upload_rag, "_store", _boom)
        assert upload_rag.retrieve_upload_chunks("s", "o", "query") == []


class TestAvailability:
    def test_disabled_flag(self, rag_env, monkeypatch):
        monkeypatch.setattr(settings, "RAG_UPLOADS_ENABLED", False)
        upload_rag.reset_for_tests()
        assert upload_rag.availability() is False

    def test_store_probe_failure_disables(self, rag_env, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("import failed")

        monkeypatch.setattr(upload_rag, "_store", _boom)
        upload_rag.reset_for_tests()
        assert upload_rag.availability() is False

    def test_enabled_probe(self, rag_env):
        upload_rag.reset_for_tests()
        assert upload_rag.availability() is True


class TestBuildBlock:
    def test_empty_chunks_returns_empty(self):
        assert upload_rag.build_retrieval_block("q", []) == ""

    def test_formats_passages_with_index_and_file(self):
        chunks = [
            {
                "text": "DCPD pricing rose 12%",
                "score": 0.81,
                "metadata": {"file_name": "a.txt", "chunk_index": 3},
            }
        ]
        block = upload_rag.build_retrieval_block("DCPD pricing", chunks)
        assert "[1]" in block
        assert "file=a.txt" in block
        assert "chunk=3" in block
        assert "QUESTION: DCPD pricing" in block
        assert "do not fabricate" in block


class TestScopeIsolation:
    """Agent + project scoping: chunks must never leak across agents or
    projects, and project-scoped cross-session retrieval must work."""

    def test_agent_isolation(self, rag_env):
        upload_rag.index_upload_text(
            "/api/uploads/a.txt", "sess-1", "org-1", LONG_TEXT,
            file_name="a.txt", agent="agent-alpha",
        )
        upload_rag.index_upload_text(
            "/api/uploads/b.txt", "sess-1", "org-1", LONG_TEXT,
            file_name="b.txt", agent="agent-beta",
        )
        chunks_a = upload_rag.retrieve_upload_chunks(
            "sess-1", "org-1", "DCPD resin", agent="agent-alpha"
        )
        assert chunks_a
        assert all(c["metadata"]["agent"] == "agent-alpha" for c in chunks_a)
        chunks_b = upload_rag.retrieve_upload_chunks(
            "sess-1", "org-1", "DCPD resin", agent="agent-beta"
        )
        assert chunks_b
        assert all(c["metadata"]["agent"] == "agent-beta" for c in chunks_b)

    def test_project_isolation(self, rag_env):
        upload_rag.index_upload_text(
            "/api/uploads/a.txt", "sess-1", "org-1", LONG_TEXT,
            file_name="a.txt", agent="agent-alpha", project_id="proj-X",
        )
        upload_rag.index_upload_text(
            "/api/uploads/b.txt", "sess-1", "org-1", LONG_TEXT,
            file_name="b.txt", agent="agent-alpha", project_id="proj-Y",
        )
        chunks_x = upload_rag.retrieve_upload_chunks(
            "sess-1", "org-1", "DCPD resin", project_id="proj-X"
        )
        assert chunks_x
        assert all(c["metadata"]["project_id"] == "proj-X" for c in chunks_x)

    def test_project_scope_cross_session(self, rag_env):
        upload_rag.index_upload_text(
            "/api/uploads/a.txt", "sess-1", "org-1", LONG_TEXT,
            file_name="a.txt", project_id="proj-X",
        )
        upload_rag.index_upload_text(
            "/api/uploads/b.txt", "sess-2", "org-1", LONG_TEXT,
            file_name="b.txt", project_id="proj-X",
        )
        upload_rag.index_upload_text(
            "/api/uploads/c.txt", "sess-3", "org-1", LONG_TEXT,
            file_name="c.txt", project_id="proj-Z",
        )
        all_x = upload_rag.retrieve_project_chunks("proj-X", "org-1", "DCPD resin")
        assert all_x
        assert all(c["metadata"]["project_id"] == "proj-X" for c in all_x)
        sessions = {c["metadata"]["session_id"] for c in all_x}
        assert sessions == {"sess-1", "sess-2"}
        z = upload_rag.retrieve_project_chunks("proj-Z", "org-1", "DCPD resin")
        assert z
        assert all(c["metadata"]["session_id"] == "sess-3" for c in z)

    def test_empty_project_scope_returns_nothing(self, rag_env):
        assert upload_rag.retrieve_project_chunks("", "org-1", "q") == []

    def test_scope_where_single_key_is_plain(self):
        assert upload_rag._scope_where(session_id="s") == {"session_id": "s"}
        assert upload_rag._scope_where(session_id="s", agent="a") == {
            "$and": [{"session_id": "s"}, {"agent": "a"}]
        }
        assert upload_rag._scope_where() == {}
