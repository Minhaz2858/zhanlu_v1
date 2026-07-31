"""Tests for the memory scoring module."""

import time
import pytest

from app.services.memory_advanced.scorer import (
    DEFAULT_ALPHA,
    DEFAULT_BETA,
    DEFAULT_GAMMA,
    MemoryHit,
    rank,
    score,
)


def test_score_blends_three_signals():
    m = MemoryHit(
        id="1", text="x", cosine=1.0, recency=1.0, importance=1.0,
        final_score=0.0, metadata={},
    )
    assert score(m) == pytest.approx(1.0)


def test_score_clamps_inputs():
    m = MemoryHit(
        id="1", text="x", cosine=2.0, recency=-0.5, importance=99.0,
        final_score=0.0, metadata={},
    )
    s = score(m)
    assert 0.0 <= s <= 1.0


def test_score_weights_sum_to_one():
    assert DEFAULT_ALPHA + DEFAULT_BETA + DEFAULT_GAMMA == pytest.approx(1.0)


def test_rank_orders_by_final_score():
    now = time.time()
    candidates = [
        MemoryHit(id="a", text="x", cosine=0.9, recency=0.0, importance=0.0,
                  final_score=0.0, metadata={"created_at": now}),
        MemoryHit(id="b", text="x", cosine=0.3, recency=1.0, importance=0.9,
                  final_score=0.0, metadata={"created_at": now}),
        MemoryHit(id="c", text="x", cosine=0.6, recency=0.5, importance=0.5,
                  final_score=0.0, metadata={"created_at": now}),
    ]
    ranked = rank(candidates)
    # "a" (high cosine) should beat "b" (medium cosine, high recency+imp)
    # at default weights because alpha dominates.
    assert ranked[0].id in ("a", "c")
    assert all(r.final_score > 0 for r in ranked)


def test_rank_handles_missing_timestamp():
    candidates = [
        MemoryHit(id="a", text="x", cosine=0.5, recency=0.0, importance=0.5,
                  final_score=0.0, metadata={}),
    ]
    ranked = rank(candidates)
    # When no timestamp is available, recency defaults to 0.5 so the
    # final score is still computable.
    assert ranked[0].final_score > 0
