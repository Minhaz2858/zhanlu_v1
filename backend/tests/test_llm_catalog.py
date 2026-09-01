"""Tests for the well-known LLM provider catalog (Kimi K2 / MiniMax M2).

Covers:
  - Catalog entries: model_id, provider, base_url, context_window
  - should_seed_catalog() mode semantics (skip / missing-only / default)
  - upsert_catalog_row() create + de-dupe by model_id
  - seed_llm_catalog() idempotency + tenant scoping

Red-first: fails until ``app/services/llm_catalog.py`` exists.
"""
import os
import sys
from unittest.mock import patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.base import TimestampedBase
from app.models.llm_model import LlmModel
from app.services.llm_catalog import (
    DEFAULT_APP,
    DEFAULT_ORG,
    LLM_CATALOG,
    seed_llm_catalog,
    should_seed_catalog,
    upsert_catalog_row,
)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db():
    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _entry(model_id: str) -> dict:
    for e in LLM_CATALOG:
        if e["model_id"] == model_id:
            return e
    raise AssertionError(f"catalog missing entry for {model_id}")


# ── Catalog data ──────────────────────────────────────────────────────

def test_catalog_has_kimi_k2_moonshot():
    entry = _entry("kimi-k2")
    assert entry["provider"] == "moonshot"
    assert entry["base_url"] == "https://api.moonshot.cn/v1"
    assert entry["context_window"] == 131072
    assert entry["is_private"] is False


def test_catalog_has_minimax_m2():
    entry = _entry("MiniMax-M2")
    assert entry["provider"] == "minimax"
    assert entry["base_url"] == "https://api.minimax.io/v1"
    assert entry["context_window"] == 204800
    assert entry["is_private"] is False


def test_catalog_entries_never_carry_secrets():
    for e in LLM_CATALOG:
        assert "api_key" not in e or not e.get("api_key")


# ── should_seed_catalog ───────────────────────────────────────────────

def test_should_seed_catalog_skip_mode_disables():
    assert should_seed_catalog("skip") is False
    assert should_seed_catalog("SKIP") is False
    assert should_seed_catalog(" skip ") is False


def test_should_seed_catalog_missing_only_enables():
    assert should_seed_catalog("missing-only") is True


def test_should_seed_catalog_defaults_to_missing_only():
    with patch.object(settings, "LLM_CATALOG_SEED_MODE", ""):
        assert should_seed_catalog() is True
    assert should_seed_catalog() is True


# ── upsert_catalog_row ────────────────────────────────────────────────

def test_upsert_creates_row_when_missing(db):
    row = upsert_catalog_row(db, _entry("kimi-k2"))
    assert row is not None
    assert row.model_id == "kimi-k2"
    assert row.provider == "moonshot"
    assert row.base_url == "https://api.moonshot.cn/v1"
    assert row.context_window == 131072
    assert row.api_key is None  # keys are never seeded
    assert row.is_default is False
    assert row.enabled is True
    assert row.org_id == DEFAULT_ORG
    assert row.app_id == DEFAULT_APP


def test_upsert_dedupes_by_model_id(db):
    first = upsert_catalog_row(db, _entry("kimi-k2"))
    second = upsert_catalog_row(db, _entry("kimi-k2"))
    assert first is not None
    assert second is None
    count = (
        db.query(LlmModel)
        .filter(LlmModel.model_id == "kimi-k2", LlmModel.is_deleted.is_(False))
        .count()
    )
    assert count == 1


def test_upsert_never_overwrites_admin_edits(db):
    upsert_catalog_row(db, _entry("kimi-k2"))
    row = db.query(LlmModel).filter(LlmModel.model_id == "kimi-k2").first()
    row.base_url = "https://custom.example.com/v1"
    db.flush()
    again = upsert_catalog_row(db, _entry("kimi-k2"))
    assert again is None
    db.refresh(row)
    assert row.base_url == "https://custom.example.com/v1"


# ── seed_llm_catalog ──────────────────────────────────────────────────

def test_seed_llm_catalog_inserts_all_entries(db):
    rows = seed_llm_catalog(db)
    assert len(rows) == len(LLM_CATALOG)
    assert {r.model_id for r in rows} == {"kimi-k2", "MiniMax-M2"}
    assert (
        db.query(LlmModel).filter(LlmModel.is_deleted.is_(False)).count()
        == len(LLM_CATALOG)
    )


def test_seed_llm_catalog_is_idempotent(db):
    seed_llm_catalog(db)
    again = seed_llm_catalog(db)
    assert again == []
    assert (
        db.query(LlmModel).filter(LlmModel.is_deleted.is_(False)).count()
        == len(LLM_CATALOG)
    )


def test_seed_scoped_to_default_tenant(db):
    seed_llm_catalog(db, org_id="acme", app_id="acme-app")
    rows = db.query(LlmModel).all()
    assert len(rows) == len(LLM_CATALOG)
    assert all(r.org_id == "acme" and r.app_id == "acme-app" for r in rows)
