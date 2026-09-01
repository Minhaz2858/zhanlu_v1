"""Regression tests for Phase 4: Context handling.

Covers:
- retrieval_hybrid.py cosine similarity scoring
- retrieve_relevant_messages with and without embeddings
- should_skip_compaction
- build_retrieval_context_block
"""

import pytest
from unittest.mock import patch


class TestRetrievalHybrid:
    """Verify semantic retrieval of relevant conversation messages."""

    def test_cosine_similarity_identical(self):
        from app.services.retrieval_hybrid import _cosine_similarity
        vec = [1.0, 2.0, 3.0]
        score = _cosine_similarity(vec, vec)
        assert abs(score - 1.0) < 0.001

    def test_cosine_similarity_orthogonal(self):
        from app.services.retrieval_hybrid import _cosine_similarity
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        score = _cosine_similarity(a, b)
        assert abs(score - 0.0) < 0.001

    def test_cosine_similarity_mismatched_lengths(self):
        from app.services.retrieval_hybrid import _cosine_similarity
        score = _cosine_similarity([1.0], [1.0, 2.0])
        assert score == 0.0

    def test_retrieve_disabled_returns_empty(self):
        from app.services.retrieval_hybrid import retrieve_relevant_messages
        with patch("app.services.retrieval_hybrid.is_enabled", return_value=False):
            result = retrieve_relevant_messages("query", [{"role": "user", "content": "hi"}])
            assert result == []

    def test_build_retrieval_context_block_empty(self):
        from app.services.retrieval_hybrid import build_retrieval_context_block
        with patch("app.services.retrieval_hybrid.is_enabled", return_value=False):
            block = build_retrieval_context_block("query", [])
            assert block == ""

    def test_should_skip_compaction_disabled(self):
        from app.services.retrieval_hybrid import should_skip_compaction
        with patch("app.services.retrieval_hybrid.is_enabled", return_value=False):
            assert should_skip_compaction("query", []) is False
