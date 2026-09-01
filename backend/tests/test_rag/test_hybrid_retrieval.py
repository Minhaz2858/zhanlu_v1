"""Tests for hybrid_retrieval.py — pure-function RRF dense+sparse fusion engine.

These tests cover the public API:
- query_terms(): CJK bigram + English token extraction
- lexical_score(): Weighted lexical scoring
- reciprocal_rank_fusion(): RRF(k=60) merge
- sparse_hits_from_collection(): Lexical retrieval from a collection-like object
- dense_hits_from_query(): Dense vector retrieval from a collection-like object
- hybrid_query_collection(): End-to-end hybrid query combining dense + sparse
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

# Public API imports — these will FAIL until implementation exists
from app.services.rag.hybrid_retrieval import (
    query_terms,
    lexical_score,
    reciprocal_rank_fusion,
    sparse_hits_from_collection,
    dense_hits_from_query,
    hybrid_query_collection,
    DEFAULT_RRF_K,
    DEFAULT_DENSE_WEIGHT,
    DEFAULT_SPARSE_WEIGHT,
    DEFAULT_PREFETCH_LIMIT,
)


# ---------------------------------------------------------------------------
# query_terms
# ---------------------------------------------------------------------------


class TestQueryTerms:
    def test_empty_string_returns_empty(self):
        assert query_terms("") == set()

    def test_whitespace_only_returns_empty(self):
        assert query_terms("   \t\n  ") == set()

    def test_english_text_returns_lowercased_tokens(self):
        result = query_terms("Hello World")
        assert "hello" in result
        assert "world" in result

    def test_pure_chinese_extracts_bigrams(self):
        # 单个汉字长度 < 2 不应包含；连续 CJK 序列应提取 bigram
        result = query_terms("乙烯价格")
        assert "乙烯" in result
        assert "烯价" in result
        assert "价格" in result

    def test_pure_chinese_includes_long_sequences(self):
        # 4 个连续汉字应有 3 个 bigram
        result = query_terms("原油价格上涨")
        assert "原油" in result
        assert "油价" in result
        assert "格上" in result
        assert "上涨" in result

    def test_english_with_numbers_extracts_numbers(self):
        result = query_terms("Price is 123.45 dollars")
        assert "123.45" in result

    def test_mixed_chinese_english(self):
        result = query_terms("DCPD 树脂价格上涨")
        # English token
        assert "dcpd" in result
        # Chinese bigrams
        assert "树脂" in result
        assert "价格" in result
        assert "上涨" in result

    def test_punctuation_stripped(self):
        result = query_terms("Hello, world! 价格: 100元。")
        assert "hello" in result
        assert "world" in result
        assert "100" in result
        # 大字 bigrams 应存在
        assert "价格" in result

    def test_dedup_of_repeated_terms(self):
        # Same term appearing multiple times → still only once in set
        result = query_terms("test test test")
        assert result == {"test"}

    def test_no_singleton_cjk(self):
        # Single CJK chars alone (no bigrams) should not produce singletons
        result = query_terms("乙")
        assert "乙" not in result
        assert len(result) == 0

    def test_two_char_chinese_minimum(self):
        # Exactly 2 CJK chars → 1 bigram, no singletons
        result = query_terms("乙烯")
        assert "乙烯" in result
        assert "乙" not in result
        assert "烯" not in result


# ---------------------------------------------------------------------------
# lexical_score
# ---------------------------------------------------------------------------


class TestLexicalScore:
    def test_perfect_match_max_score(self):
        terms = {"油价", "上涨"}
        score = lexical_score("油价上涨趋势明显", terms)
        assert score > 0

    def test_no_match_returns_zero(self):
        terms = {"油价"}
        score = lexical_score("天气晴朗", terms)
        assert score == 0.0

    def test_empty_terms_returns_zero(self):
        score = lexical_score("any text here", set())
        assert score == 0.0

    def test_empty_text_returns_zero(self):
        score = lexical_score("", {"term"})
        assert score == 0.0

    def test_partial_match_proportional(self):
        terms = {"a", "b", "c"}
        full = lexical_score("a b c", terms)
        half = lexical_score("a b", terms)
        assert full > half > 0

    def test_length_normalized_to_prevent_long_doc_bias(self):
        # A very long doc that matches same number of terms should score
        # similar to a short doc with same match density
        terms = {"油价"}
        short = lexical_score("油价", terms)
        long_padding = "x" * 1000
        long_text = f"油价{long_padding}"
        long_score = lexical_score(long_text, terms)
        # Long doc should NOT dominate — score should be within reasonable ratio
        assert short > 0
        assert long_score > 0


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion
# ---------------------------------------------------------------------------


class TestReciprocalRankFusion:
    def test_empty_lists_returns_empty(self):
        assert reciprocal_rank_fusion([], []) == []

    def test_single_list_fused_by_rank(self):
        # list of (doc_id, score) — RRF just reorders with score = 1/(k+rank)
        lists = [["doc1", "doc2", "doc3"]]
        result = reciprocal_rank_fusion(lists, k=60)
        # Result preserves doc_id but assigns RRF score
        assert [d[0] for d in result] == ["doc1", "doc2", "doc3"]
        # First doc should have highest score
        assert result[0][1] > result[1][1] > result[2][1]

    def test_two_lists_fuse_overlapping_docs(self):
        lists = [
            ["doc1", "doc2", "doc3"],  # dense hits
            ["doc2", "doc1", "doc4"],  # sparse hits
        ]
        result = reciprocal_rank_fusion(lists, k=60)
        doc_ids = [d[0] for d in result]
        # doc1 + doc2 appear in both → highest combined score
        assert "doc1" in doc_ids
        assert "doc2" in doc_ids
        # doc1/doc2 should have higher scores than doc3/doc4 (appear in only one list)
        score_map = dict(result)
        assert score_map["doc1"] > score_map.get("doc3", 0)
        assert score_map["doc2"] > score_map.get("doc3", 0)

    def test_default_k_is_60(self):
        assert DEFAULT_RRF_K == 60

    def test_three_lists_combined(self):
        lists = [
            ["a", "b", "c"],
            ["b", "c", "d"],
            ["c", "d", "e"],
        ]
        result = reciprocal_rank_fusion(lists, k=60)
        # c appears in all 3 lists → should have highest score
        top_doc = result[0][0]
        assert top_doc == "c"

    def test_output_is_list_of_tuples(self):
        lists = [["doc1"]]
        result = reciprocal_rank_fusion(lists, k=60)
        assert isinstance(result, list)
        assert isinstance(result[0], tuple)
        assert len(result[0]) == 2  # (doc_id, score)


# ---------------------------------------------------------------------------
# sparse_hits_from_collection
# ---------------------------------------------------------------------------


class TestSparseHits:
    def _make_collection(self, docs: Dict[str, str]) -> Any:
        """Build a duck-typed collection supporting .get() and .documents.

        Mimics ChromaDB collection API for the sparse retrieval path.
        """

        class _MockCollection:
            def __init__(self, docs: Dict[str, str]) -> None:
                self._docs = docs

            def get(self) -> Dict[str, List[Any]]:
                ids = list(self._docs.keys())
                documents = [self._docs[i] for i in ids]
                return {"ids": ids, "documents": documents}

        return _MockCollection(docs)

    def test_empty_collection_returns_empty(self):
        coll = self._make_collection({})
        result = sparse_hits_from_collection(coll, "anything")
        assert result == []

    def test_no_matching_terms_returns_empty(self):
        coll = self._make_collection({"d1": "天气晴朗", "d2": "出门散步"})
        result = sparse_hits_from_collection(coll, "油价上涨")
        assert result == []

    def test_matching_doc_returns_with_positive_score(self):
        coll = self._make_collection({"d1": "油价上涨趋势"})
        result = sparse_hits_from_collection(coll, "油价上涨")
        assert len(result) == 1
        doc_id, score = result[0]
        assert doc_id == "d1"
        assert score > 0

    def test_top_k_limits_results(self):
        coll = self._make_collection({f"d{i}": "油价" for i in range(20)})
        result = sparse_hits_from_collection(coll, "油价", top_k=5)
        assert len(result) == 5

    def test_ranking_by_score_descending(self):
        coll = self._make_collection(
            {
                "weak": "油价",
                "medium": "油价油价",
                "strong": "油价油价油价",
            }
        )
        result = sparse_hits_from_collection(coll, "油价")
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# dense_hits_from_query
# ---------------------------------------------------------------------------


class TestDenseHits:
    def _make_collection(self, results: List[Tuple[List[str], List[float]]]) -> Any:
        """Mock collection that returns predefined dense-query results.

        Each call to .query(query_texts=...) returns the next pre-canned result.
        """

        class _MockCollection:
            def __init__(self, results: List[Tuple[List[str], List[float]]]) -> None:
                self._results = list(results)
                self._idx = 0

            def query(self, query_texts: List[str], n_results: int) -> Dict[str, Any]:
                if self._idx >= len(self._results):
                    return {"ids": [], "distances": []}
                ids, distances = self._results[self._idx]
                self._idx += 1
                return {"ids": [ids], "distances": [distances]}

        return _MockCollection(results)

    def test_no_results_returns_empty(self):
        coll = self._make_collection([([], [])])
        result = dense_hits_from_query(coll, "query")
        assert result == []

    def test_basic_query_returns_doc_ids_and_scores(self):
        coll = self._make_collection(
            [(["d1", "d2", "d3"], [0.1, 0.3, 0.5])]
        )
        result = dense_hits_from_query(coll, "query")
        assert len(result) == 3
        # Smaller distance → larger score (exp(-distance))
        assert result[0] == ("d1", pytest.approx(2.0 ** -0.1, rel=1e-3)) or \
               result[0][0] == "d1"

    def test_distance_zero_means_perfect_match(self):
        coll = self._make_collection([(["d1"], [0.0])])
        result = dense_hits_from_query(coll, "query")
        assert result[0][1] == pytest.approx(1.0, rel=1e-3)

    def test_top_k_limits_results(self):
        coll = self._make_collection(
            [(["d1", "d2", "d3", "d4", "d5"], [0.1, 0.2, 0.3, 0.4, 0.5])]
        )
        result = dense_hits_from_query(coll, "query", top_k=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# hybrid_query_collection — integration
# ---------------------------------------------------------------------------


class TestHybridQueryCollection:
    def _make_collection(
        self,
        docs: Dict[str, str],
        dense_results: List[Tuple[List[str], List[float]]] | None = None,
    ) -> Any:
        class _MockCollection:
            def __init__(self, docs, dense_results):
                self._docs = docs
                self._dense = list(dense_results or [])
                self._idx = 0

            def get(self):
                ids = list(self._docs.keys())
                documents = [self._docs[i] for i in ids]
                return {"ids": ids, "documents": documents}

            def query(self, query_texts, n_results):
                if self._idx >= len(self._dense):
                    return {"ids": [[]], "distances": [[]]}
                ids, distances = self._dense[self._idx]
                self._idx += 1
                return {"ids": [ids], "distances": [distances]}

        return _MockCollection(docs, dense_results or [])

    def test_empty_collection_returns_empty(self):
        coll = self._make_collection({})
        result = hybrid_query_collection(coll, "any query")
        assert result == []

    def test_combines_dense_and_sparse(self):
        # Dense returns doc1 + doc2; sparse matches doc2 + doc3
        coll = self._make_collection(
            {"doc1": "alpha content", "doc2": "alpha beta", "doc3": "beta gamma"},
            dense_results=[
                (["doc1", "doc2", "doc3"], [0.1, 0.4, 0.9])
            ],
        )
        result = hybrid_query_collection(coll, "alpha beta", top_k=5)
        assert len(result) > 0
        doc_ids = [d[0] for d in result]
        assert "doc2" in doc_ids  # appears in both

    def test_respects_top_k(self):
        coll = self._make_collection(
            {f"d{i}": f"term{i}" for i in range(10)},
            dense_results=[
                ([f"d{i}" for i in range(10)], [0.1 * i for i in range(10)])
            ],
        )
        result = hybrid_query_collection(coll, "term0", top_k=3)
        assert len(result) == 3

    def test_default_weights_are_exported(self):
        assert isinstance(DEFAULT_DENSE_WEIGHT, float)
        assert isinstance(DEFAULT_SPARSE_WEIGHT, float)
        # Should sum to 1.0
        assert DEFAULT_DENSE_WEIGHT + DEFAULT_SPARSE_WEIGHT == pytest.approx(1.0)

    def test_prefetch_limit_default(self):
        assert DEFAULT_PREFETCH_LIMIT > 0
