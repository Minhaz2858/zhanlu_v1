"""Unit tests for fast_path.try_fast_path."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.services.project_knowledge.fast_path import (
    try_fast_path, build_cached_system_block,
)
from app.services.project_knowledge.models import CacheQueryResult


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _enable():
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    settings.PROJECT_KNOWLEDGE_QWEN_FAST_PATH = True


def test_fast_path_disabled_when_flag_off(db):
    settings.PROJECT_KNOWLEDGE_QWEN_FAST_PATH = False
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    assert try_fast_path(db, "p1", "isoprene", "qwen3.6-27b") is None


def test_fast_path_skips_non_qwen(db):
    _enable()
    assert try_fast_path(db, "p1", "isoprene", "gpt-4") is None


def test_fast_path_hits_on_qwen(db):
    _enable()
    # Resolver does exact-token matching; use a clean question.
    r = try_fast_path(db, "p1", "isoprene", "qwen3.6-27b")
    assert r is not None
    assert r.kind == "product"


def test_fast_path_qwen3_prefix(db):
    _enable()
    r = try_fast_path(db, "p1", "异戊二烯", "Qwen3-Max")
    assert r is not None
    assert r.kind == "product"


def test_fast_path_case_insensitive(db):
    _enable()
    r1 = try_fast_path(db, "p1", "isoprene", "Qwen3.6-27B")
    r2 = try_fast_path(db, "p1", "isoprene", "qwen3.6-27b")
    assert r1 is not None and r2 is not None


def test_build_cached_system_block_contains_kind():
    r = CacheQueryResult(kind="product", context_block="abc", confidence=0.9)
    block = build_cached_system_block(r)
    assert "kind: product" in block
    assert "abc" in block
    assert "confidence: 0.9" in block


def test_build_cached_system_block_none_safe():
    assert build_cached_system_block(None) == ""