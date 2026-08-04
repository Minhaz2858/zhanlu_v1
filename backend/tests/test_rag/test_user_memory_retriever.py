"""Tests for user_memory_retriever.

Provides per-user long-term memory snapshot retrieval against the
``user_memory`` Chroma collection. Built on top of ``knowledge_base``.

Public API:
    UserMemoryEntry (dataclass)
    build_user_context(user_id, query, max_chars=900) -> str
    retrieve_user_memory(user_id, query, top_k=5) -> List[UserMemoryEntry]
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.rag import user_memory_retriever
from app.services.rag.user_memory_retriever import (
    KNOWN_MEMORY_TYPES,
    UserMemoryEntry,
    build_user_context,
    retrieve_user_memory,
)


@pytest.fixture
def mock_kb(monkeypatch):
    fake = MagicMock()
    fake_collection = MagicMock()
    fake_collection.query.return_value = {
        "documents": [[
            "User previously asked about DCPD price volatility.",
            "User prefers short answers.",
        ]],
        "metadatas": [[
            {"user_id": "u1", "memory_type": "preference", "topic": "format"},
            {"user_id": "u1", "memory_type": "fact", "topic": "DCPD"},
        ]],
        "distances": [[0.1, 0.3]],
    }
    fake.get_collection.return_value = fake_collection
    monkeypatch.setattr(user_memory_retriever, "_get_kb", lambda: fake)
    return fake


class TestUserMemoryEntry:
    def test_formatted_default(self):
        e = UserMemoryEntry(
            text="hello",
            memory_type="fact",
            user_id="u1",
            topic="DCPD",
            score=0.9,
        )
        out = e.formatted()
        assert "fact" in out
        assert "hello" in out

    def test_known_types_constant(self):
        assert "fact" in KNOWN_MEMORY_TYPES
        assert "preference" in KNOWN_MEMORY_TYPES


class TestRetrieveUserMemory:
    def test_returns_entries(self, mock_kb):
        out = retrieve_user_memory("u1", "DCPD price", top_k=3)
        assert len(out) >= 1
        assert all(isinstance(e, UserMemoryEntry) for e in out)

    def test_populates_fields(self, mock_kb):
        out = retrieve_user_memory("u1", "DCPD price", top_k=3)
        e = out[0]
        assert e.memory_type in ("fact", "preference")
        assert e.user_id == "u1"
        assert e.score > 0

    def test_empty_query_returns_empty(self, mock_kb):
        assert retrieve_user_memory("u1", "") == []

    def test_kb_unavailable_returns_empty(self, monkeypatch):
        monkeypatch.setattr(user_memory_retriever, "_get_kb", lambda: None)
        assert retrieve_user_memory("u1", "anything") == []


class TestBuildUserContext:
    def test_returns_header_and_chunks(self, mock_kb):
        out = build_user_context("u1", "DCPD", max_chars=2000)
        assert "User Memory" in out
        assert "DCPD" in out

    def test_empty_query_returns_empty(self, mock_kb):
        assert build_user_context("u1", "") == ""

    def test_no_results_returns_empty(self, mock_kb):
        mock_kb.get_collection.return_value.query.return_value = {
            "documents": [[]], "metadatas": [[]], "distances": [[]]
        }
        assert build_user_context("u1", "nothing") == ""

    def test_max_chars_limit(self, mock_kb):
        # Force lots of long entries
        mock_kb.get_collection.return_value.query.return_value = {
            "documents": [["x" * 1000] * 10],
            "metadatas": [[
                {"user_id": "u1", "memory_type": "fact", "topic": "t"}
            ] * 10],
            "distances": [[0.1] * 10],
        }
        out = build_user_context("u1", "x", max_chars=200)
        assert len(out) <= 500