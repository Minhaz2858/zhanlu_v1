"""Tests for the ask_data_agent result cache."""
import time
import pytest
from app.services.query_result_cache import (
    get_cached_result,
    put_result,
    invalidate,
    _normalize_question,
)


def test_normalize_question():
    assert _normalize_question("July 2026 Sales Volume") == _normalize_question("july 2026 sales volume")
    assert _normalize_question("  July   2026   sales  ") == _normalize_question("July 2026 sales")
    assert _normalize_question("sales (volume + revenue)") == _normalize_question("sales volume revenue")


def test_cache_miss_returns_none():
    invalidate()  # clear
    result = get_cached_result("nonexistent question", "kb1")
    assert result is None


def test_cache_hit_returns_result():
    invalidate()
    put_result(
        question="July 2026 sales volume",
        kb_id="kb1",
        result={"success": True, "rows": [{"volume": 5000}], "answer": "5K tons"},
    )
    hit = get_cached_result("July 2026 sales volume", "kb1")
    assert hit is not None
    assert hit["success"] is True
    assert hit["rows"][0]["volume"] == 5000


def test_cache_hit_normalization():
    invalidate()
    put_result(
        question="July 2026 SALES volume",
        kb_id="kb1",
        result={"success": True, "rows": [{"volume": 5000}]},
    )
    # Different casing/whitespace should still hit
    hit = get_cached_result("july 2026 sales volume", "kb1")
    assert hit is not None
    assert hit["rows"][0]["volume"] == 5000


def test_cache_miss_different_kb():
    invalidate()
    put_result(
        question="July 2026 sales",
        kb_id="kb1",
        result={"success": True, "rows": [{"x": 1}]},
    )
    miss = get_cached_result("July 2026 sales", "kb2")
    assert miss is None


def test_cache_invalidation_by_kb():
    invalidate()
    put_result("q1", "kb1", {"success": True, "rows": [{"x": 1}], "source_id": "kb1"})
    put_result("q2", "kb2", {"success": True, "rows": [{"x": 2}], "source_id": "kb2"})
    invalidate(kb_id="kb1")
    assert get_cached_result("q1", "kb1") is None
    assert get_cached_result("q2", "kb2") is not None


def test_cache_age_included():
    invalidate()
    put_result("q1", "kb1", {"success": True, "rows": [{"x": 1}], "answer": "ok"})
    hit = get_cached_result("q1", "kb1")
    assert hit is not None
    assert "_cache_age_s" in hit
    assert hit["_cache_age_s"] < 5  # just stored


def test_dont_cache_empty_results():
    invalidate()
    put_result("q_empty", "kb1", {"success": True, "rows": [], "answer": "none"})
    miss = get_cached_result("q_empty", "kb1")
    assert miss is None


def test_dont_cache_failures():
    invalidate()
    put_result("q_fail", "kb1", {"success": False, "rows": [{"x": 1}], "error": "oops"})
    miss = get_cached_result("q_fail", "kb1")
    assert miss is None
