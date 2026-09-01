"""Regression tests for retrieval_hybrid.py (Part 2 — Phase 4 context quality)."""

from unittest.mock import patch

from app.services import retrieval_hybrid as rh


class TestCosineSimilarity:
    """Tests for _cosine_similarity (pure-math, no numpy)."""

    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        score = rh._cosine_similarity(v, v)
        assert abs(score - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        score = rh._cosine_similarity(a, b)
        assert abs(score - 0.0) < 1e-6

    def test_negative_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        score = rh._cosine_similarity(a, b)
        assert abs(score - (-1.0)) < 1e-6

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        score = rh._cosine_similarity(a, b)
        assert score == 0.0

    def test_both_zero_vectors(self):
        a = [0.0, 0.0]
        b = [0.0, 0.0]
        score = rh._cosine_similarity(a, b)
        assert score == 0.0

    def test_mismatched_lengths(self):
        a = [1.0, 2.0]
        b = [1.0, 2.0, 3.0]
        score = rh._cosine_similarity(a, b)
        assert score == 0.0

    def test_empty_lists(self):
        assert rh._cosine_similarity([], []) == 0.0
        assert rh._cosine_similarity([1.0], []) == 0.0

    def test_high_dimensional_near_identical(self):
        a = [0.1] * 100
        b = [0.1] * 100
        score = rh._cosine_similarity(a, b)
        assert abs(score - 1.0) < 1e-6


class TestShouldSkipCompaction:
    """Tests for should_skip_compaction."""

    def test_skip_when_enough_relevant(self):
        history = [{"role": "user", "content": "hello", "embedding": [0.1, 0.2, 0.3]}]
        with patch.object(rh, "is_enabled", return_value=True):
            with patch.object(rh, "min_score", return_value=0.0):
                with patch("app.services.llm_service.get_embedding",
                           return_value=[0.1, 0.2, 0.3]):
                    result = rh.should_skip_compaction("hello", history, min_relevant=1)
                    assert result is True

    def test_no_skip_when_not_enough(self):
        with patch.object(rh, "is_enabled", return_value=False):
            result = rh.should_skip_compaction("query", [], min_relevant=3)
            assert result is False


class TestRetrieveRelevantMessages:
    """Tests for retrieve_relevant_messages."""

    def test_returns_empty_when_disabled(self):
        with patch.object(rh, "is_enabled", return_value=False):
            result = rh.retrieve_relevant_messages("hello", [{"role": "user", "content": "hi"}])
            assert result == []

    def test_returns_empty_for_empty_history(self):
        with patch.object(rh, "is_enabled", return_value=True):
            result = rh.retrieve_relevant_messages("query", [])
            assert result == []

    def test_returns_empty_when_embedder_fails(self):
        history = [{"role": "user", "content": "msg", "embedding": [0.1, 0.2]}]
        with patch.object(rh, "is_enabled", return_value=True):
            with patch("app.services.llm_service.get_embedding",
                       side_effect=Exception("no embedder")):
                result = rh.retrieve_relevant_messages("q", history)
                assert result == []

    def test_retrieves_relevant_with_embeddings(self):
        history = [
            {"role": "user", "content": "weather", "embedding": [1.0, 0.0, 0.0]},
            {"role": "user", "content": "irrelevant", "embedding": [-1.0, 0.0, 0.0]},
            {"role": "assistant", "content": "market", "embedding": [0.9, 0.1, 0.0]},
        ]
        with patch.object(rh, "is_enabled", return_value=True):
            with patch.object(rh, "min_score", return_value=0.0):
                with patch("app.services.llm_service.get_embedding",
                           return_value=[1.0, 0.0, 0.0]):
                    result = rh.retrieve_relevant_messages("query", history, top_k=2)
                    assert len(result) == 2
                    # highest score should be first
                    assert result[0]["_retrieval_score"] >= result[1]["_retrieval_score"]

    def test_messages_without_embedding_skipped(self):
        history = [
            {"role": "user", "content": "no embedding"},
            {"role": "user", "content": "has embedding", "embedding": [0.5, 0.5]},
        ]
        with patch.object(rh, "is_enabled", return_value=True):
            with patch("app.services.llm_service.get_embedding",
                       return_value=[0.5, 0.5]):
                result = rh.retrieve_relevant_messages("query", history)
                assert len(result) == 1
                assert result[0]["content"] == "has embedding"


class TestBuildRetrievalContextBlock:
    """Tests for build_retrieval_context_block."""

    def test_returns_empty_when_disabled(self):
        with patch.object(rh, "is_enabled", return_value=False):
            result = rh.build_retrieval_context_block("q", [])
            assert result == ""

    def test_returns_formatted_block_when_enabled(self):
        history = [
            {"role": "user", "content": "hello", "embedding": [0.5, 0.5]},
        ]
        with patch.object(rh, "is_enabled", return_value=True):
            with patch("app.services.llm_service.get_embedding",
                       return_value=[0.5, 0.5]):
                result = rh.build_retrieval_context_block("q", history)
                assert "Conversation History" in result
                assert "hello" in result
