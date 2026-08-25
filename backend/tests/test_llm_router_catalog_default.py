"""Tests for ``resolve_catalog_default_llm`` (used by background/system calls).

Covers:
  - Returns the catalog default LLMEndpoint when a default row exists
  - Returns None when no default exists (background falls back to legacy)
  - TTL cache: repeated calls within 60s do NOT re-query the DB
  - ``clear_catalog_default_cache`` forces a fresh query
"""
import os
import sys
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services import llm_router
from app.services.llm_router import (
    LLMEndpoint,
    clear_catalog_default_cache,
    resolve_catalog_default_llm,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_catalog_default_cache()
    yield
    clear_catalog_default_cache()


class _FakeRow:
    def __init__(self, **kw):
        self.id = "row-1"
        self.name = "Default Model"
        self.model_id = "default-model"
        self.provider = "test"
        self.base_url = "https://api.test.com/v1"
        self.api_key = None  # → falls back to settings.OPENAI_API_KEY
        self.is_private = False
        self.enabled = True
        self.is_default = True
        self.is_deleted = False
        self.context_window = 32768
        self.max_output_tokens = 8192
        self.supports_structured_tool_calls = True
        self.bypass_hallucination_guardrail = False
        for k, v in kw.items():
            setattr(self, k, v)


def _fake_session(row):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = row
    return session


def _patch_session(row):
    """Patch the SessionLocal used inside ``resolve_catalog_default_llm``
    (imported from ``app.database`` at call time)."""
    return patch("app.database.SessionLocal", return_value=_fake_session(row))


def test_returns_catalog_default_endpoint():
    row = _FakeRow()
    with (
        _patch_session(row),
        patch("app.services.llm_router.settings") as mock_settings,
    ):
        mock_settings.OPENAI_API_KEY = "sk-legacy"
        endpoint = resolve_catalog_default_llm()

    assert isinstance(endpoint, LLMEndpoint)
    assert endpoint.model_id == "default-model"
    assert endpoint.base_url == "https://api.test.com/v1"
    # api_key row was None → legacy fallback key used
    assert endpoint.api_key == "sk-legacy"


def test_returns_none_when_no_default():
    with (
        _patch_session(None),
        patch("app.services.llm_router.settings"),
    ):
        endpoint = resolve_catalog_default_llm()

    assert endpoint is None


def test_ttl_cache_avoids_second_db_query():
    """A second call within the TTL must NOT hit the DB again."""
    row = _FakeRow()
    session_factory = MagicMock(return_value=_fake_session(row))
    with (
        patch("app.database.SessionLocal", session_factory),
        patch("app.services.llm_router.settings"),
    ):
        first = resolve_catalog_default_llm()
        second = resolve_catalog_default_llm()

    assert first is second  # same cached object
    assert session_factory.call_count == 1


def test_clear_cache_forces_fresh_query():
    row = _FakeRow()
    session_factory = MagicMock(return_value=_fake_session(row))
    with (
        patch("app.database.SessionLocal", session_factory),
        patch("app.services.llm_router.settings"),
    ):
        resolve_catalog_default_llm()
        resolve_catalog_default_llm()
        assert session_factory.call_count == 1

        clear_catalog_default_cache()
        resolve_catalog_default_llm()
        assert session_factory.call_count == 2


def test_query_failure_is_non_fatal():
    """A DB error inside the resolver must never raise — background calls
    fall back to the legacy global provider instead of crashing."""
    session = MagicMock()
    session.query.side_effect = RuntimeError("db down")
    with (
        patch("app.database.SessionLocal", return_value=session),
        patch("app.services.llm_router.settings"),
    ):
        endpoint = resolve_catalog_default_llm()

    assert endpoint is None
    session.close.assert_called_once()
