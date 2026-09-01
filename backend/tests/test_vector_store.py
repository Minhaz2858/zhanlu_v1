"""Tests for the in-process vector store (numpy backend)."""

import numpy as np
import pytest

from app.services.memory_advanced.vector_store import VectorStore


def test_vector_store_creates_and_queries():
    vs = VectorStore(backend="numpy", dim=3)
    vs.add("a", [1.0, 0.0, 0.0])
    vs.add("b", [0.0, 1.0, 0.0])
    vs.add("c", [0.7, 0.3, 0.0])
    results = vs.query([1.0, 0.0, 0.0], top_k=3)
    # "a" is the exact match, "c" is the next-closest.
    assert [r[0] for r in results[:2]] == ["a", "c"]


def test_vector_store_upsert_replaces():
    vs = VectorStore(backend="numpy", dim=2)
    vs.add("a", [1.0, 0.0])
    vs.add("a", [0.0, 1.0])
    results = vs.query([0.0, 1.0], top_k=1)
    assert results[0][0] == "a"
    # The replaced vector points at (0,1), so similarity should be ~1.
    assert results[0][1] == pytest.approx(1.0, abs=1e-4)


def test_vector_store_delete_removes():
    vs = VectorStore(backend="numpy", dim=2)
    vs.add("a", [1.0, 0.0])
    vs.add("b", [0.0, 1.0])
    vs.delete("a")
    results = vs.query([1.0, 0.0], top_k=5)
    assert all(r[0] != "a" for r in results)


def test_vector_store_handles_empty_state():
    vs = VectorStore(backend="numpy", dim=2)
    assert vs.query([1.0, 0.0]) == []
    vs.delete("never-existed")  # no-op
