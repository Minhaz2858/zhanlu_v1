"""Embeddings provider-mode tests (2026-10-15).

Bug: get_embedding inherited LLM_API_KEY as the embeddings key, so a
DeepSeek-key deployment hit api.openai.com on EVERY call → doomed 401
per embedding before the local fallback engaged. EMBEDDING_PROVIDER now
gates the remote API: local = never; auto = only with a DEDICATED
EMBEDDING_API_KEY or an explicit EMBEDDING_API_BASE; api = operator
override.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.memory_advanced import embeddings as emb


@pytest.fixture()
def no_network(monkeypatch):
    """Any remote API call fails the test — local paths must never dial."""
    def boom(*a, **kw):
        raise AssertionError("remote embeddings API was called")
    monkeypatch.setattr(emb, "_post_embeddings", boom)


# ── provider=local: never dial ───────────────────────────────────────────


def test_local_mode_never_calls_api(monkeypatch, no_network):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("LLM_API_KEY", "sk-some-llm-key")  # must NOT be used
    r = emb.get_embedding("供应商风险管理")
    assert r is not None and r.vector and r.model == "local"


def test_local_mode_batch_never_calls_api(monkeypatch, no_network):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("LLM_API_KEY", "sk-some-llm-key")
    out = emb.get_embeddings_batch(["采购订单", "销售预测"])
    assert all(v for v in out)


# ── provider=auto: dedicated key only ────────────────────────────────────


def test_auto_mode_ignores_inherited_llm_key(monkeypatch, no_network):
    """THE regression: LLM_API_KEY alone must not trigger remote calls."""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "auto")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-deepseek-key")
    r = emb.get_embedding("hello")
    assert r is not None and r.model == "local"


def test_auto_mode_uses_dedicated_key(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "auto")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-dedicated")
    captured = {}

    def fake_post(texts, model, api_base, api_key):
        captured["key"] = api_key
        return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(emb, "_post_embeddings", fake_post)
    r = emb.get_embedding("hello-dedicated-key-test", use_cache=False)
    assert r is not None and r.model != "local"
    assert captured["key"] == "sk-dedicated"


# ── provider=api: explicit override ──────────────────────────────────────


def test_api_mode_without_key_falls_local(monkeypatch, no_network):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "api")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    r = emb.get_embedding("hello")
    assert r is not None and r.model == "local"


# ── gate function unit checks ────────────────────────────────────────────


def test_gate_matrix(monkeypatch):
    # dedicated_key_set = DEDICATED embeddings key (env or caller), never
    # the inherited LLM/OPENAI key.
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "local")
    assert emb._should_call_remote_api(True, True) is False

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "auto")
    assert emb._should_call_remote_api(False, False) is False
    assert emb._should_call_remote_api(False, True) is True  # explicit base: yes
    assert emb._should_call_remote_api(True, False) is True

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "api")
    assert emb._should_call_remote_api(True, False) is True
    assert emb._should_call_remote_api(False, False) is False
