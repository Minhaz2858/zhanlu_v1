"""Tests for the upgraded document_ingestion.embedder module.

The embedder upgrade adds:
- ``RAG_EMBEDDING_MODEL`` env-based selection (bge-m3 / minilm / hash)
- ``LocalHashEmbeddingFunction`` — offline, dependency-free fallback
- ``get_embedding_function()`` — ChromaDB-compatible function accessor
- Backward compatibility with existing ``embed_texts()`` / ``embed_query()``
"""
from __future__ import annotations

import os
import threading
from typing import Any, List

import numpy as np
import pytest

from app.services.document_ingestion.embedder import (
    LocalHashEmbeddingFunction,
    embed_dim,
    embed_query,
    embed_texts,
    get_embedding_function,
    reset_for_tests,
)


# ---------------------------------------------------------------------------
# Backward compatibility — existing API still works
# ---------------------------------------------------------------------------


class TestBackwardCompatAPI:
    def test_embed_dim_returns_int(self):
        d = embed_dim()
        assert isinstance(d, int)
        assert d > 0

    def test_embed_texts_empty_returns_empty_array(self):
        out = embed_texts([])
        assert isinstance(out, np.ndarray)
        assert out.shape[0] == 0

    def test_embed_query_returns_1d_array(self, monkeypatch):
        # Monkey-patch _load_model to avoid loading real model
        class _Stub:
            def encode(self, texts, **_kw):
                return np.zeros((len(texts), 384), dtype="float32")

        monkeypatch.setattr(
            "app.services.document_ingestion.embedder._load_model",
            lambda: _Stub(),
        )
        reset_for_tests()
        out = embed_query("hello")
        assert out.ndim == 1
        assert out.shape[0] == 384


# ---------------------------------------------------------------------------
# LocalHashEmbeddingFunction — offline deterministic fallback
# ---------------------------------------------------------------------------


class TestLocalHashEmbeddingFunction:
    def test_dimension_is_256(self):
        fn = LocalHashEmbeddingFunction()
        out = fn(["hello"])
        assert len(out) == 1
        assert len(out[0]) == 256

    def test_deterministic_across_calls(self):
        fn = LocalHashEmbeddingFunction()
        a = fn(["乙烯价格"])
        b = fn(["乙烯价格"])
        assert a == b

    def test_different_texts_produce_different_vectors(self):
        fn = LocalHashEmbeddingFunction()
        a = fn(["乙烯价格上涨"])
        b = fn(["原油市场波动"])
        # Vectors should not be identical
        assert a != b

    def test_empty_text_returns_zero_vector(self):
        fn = LocalHashEmbeddingFunction()
        out = fn([""])
        assert len(out) == 1
        assert all(v == 0.0 for v in out[0])

    def test_batch_input(self):
        fn = LocalHashEmbeddingFunction()
        out = fn(["a", "b", "c"])
        assert len(out) == 3
        assert all(len(v) == 256 for v in out)

    def test_name_method(self):
        fn = LocalHashEmbeddingFunction()
        n = fn.name()
        assert isinstance(n, str)
        assert len(n) > 0

    def test_chinese_bigrams_have_nonzero_signal(self):
        fn = LocalHashEmbeddingFunction()
        out = fn(["乙烯价格"])[0]
        # At least some dimensions should be non-zero
        nonzero = sum(1 for v in out if v != 0.0)
        assert nonzero > 10

    def test_l2_normalized(self):
        import math

        fn = LocalHashEmbeddingFunction()
        out = fn(["some text here"])[0]
        norm = math.sqrt(sum(v * v for v in out))
        # Should be approximately 1.0
        assert 0.99 <= norm <= 1.01

    def test_works_with_chromadb_protocol(self):
        # ChromaDB needs __call__, embed_documents, embed_query, name
        fn = LocalHashEmbeddingFunction()
        assert callable(fn)
        assert hasattr(fn, "name")
        assert callable(getattr(fn, "embed_documents", None))
        assert callable(getattr(fn, "embed_query", None))


# ---------------------------------------------------------------------------
# get_embedding_function — env-based selection
# ---------------------------------------------------------------------------


class TestGetEmbeddingFunction:
    def teardown_method(self):
        # Clear any cached model between tests
        reset_for_tests()

    def test_default_returns_some_function(self, monkeypatch):
        # When env unset → default (MiniLM or LocalHash fallback)
        monkeypatch.delenv("RAG_EMBEDDING_MODEL", raising=False)
        fn = get_embedding_function()
        assert fn is not None
        assert hasattr(fn, "__call__")

    def test_env_hash_returns_local_hash(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDING_MODEL", "hash")
        fn = get_embedding_function()
        assert isinstance(fn, LocalHashEmbeddingFunction)

    def test_env_HASH_uppercase_returns_local_hash(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDING_MODEL", "HASH")
        fn = get_embedding_function()
        assert isinstance(fn, LocalHashEmbeddingFunction)

    def test_env_bge_m3_attempts_load(self, monkeypatch):
        # Patch the bge-m3 loader to return a mock function
        class _MockBge:
            def __call__(self, input):  # noqa: A002
                return [[0.0] * 1024 for _ in input]

            def name(self):
                return "bge-m3-mock"

            def embed_documents(self, texts):
                return [[0.0] * 1024 for _ in texts]

            def embed_query(self, input=None, text=None):
                return [[0.0] * 1024]

        # Patch the loader at the embedder module level
        monkeypatch.setenv("RAG_EMBEDDING_MODEL", "bge-m3")
        monkeypatch.setattr(
            "app.services.document_ingestion.embedder._load_bge_m3",
            lambda: _MockBge(),
        )
        fn = get_embedding_function()
        assert fn is not None
        assert fn.name() == "bge-m3-mock"

    def test_env_bge_m3_failure_falls_back_to_hash(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDING_MODEL", "bge-m3")
        monkeypatch.setattr(
            "app.services.document_ingestion.embedder._load_bge_m3",
            lambda: (_ for _ in ()).throw(RuntimeError("bge-m3 unavailable")),
        )
        fn = get_embedding_function()
        # Should gracefully fall back to LocalHash
        assert isinstance(fn, LocalHashEmbeddingFunction)

    def test_unknown_env_value_falls_back_to_hash(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDING_MODEL", "some-unknown-model")
        fn = get_embedding_function()
        assert isinstance(fn, LocalHashEmbeddingFunction)
