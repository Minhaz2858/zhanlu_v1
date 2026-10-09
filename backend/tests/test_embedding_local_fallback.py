"""Local embedding fallback tests (no EMBEDDING_API_KEY required)."""
import pytest

from app.services.memory_advanced import embeddings as emb


@pytest.fixture(autouse=True)
def _no_api(monkeypatch):
    """Simulate a deployment with no embedding API configured."""
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")


def test_no_key_returns_none_from_api_path_but_fallback_exists():
    """get_embedding must not return None when the local fallback is on —
    it should return a real vector (bge-m3 cached in this container)."""
    res = emb.get_embedding("revenue by region last quarter")
    assert res is not None
    assert res.vector and len(res.vector) > 64
    assert res.model == "local"


def test_local_vectors_are_normalized_and_consistent():
    v1 = emb._local_embed(["annual recurring revenue"])
    v2 = emb._local_embed(["annual recurring revenue"])
    assert len(v1[0]) == len(v2[0])
    # normalized: self-dot ≈ 1
    dot = sum(a * b for a, b in zip(v1[0], v2[0]))
    assert abs(dot - 1.0) < 0.01


def test_semantic_similarity_local_model():
    """The local model must actually separate related from unrelated text —
    that is the whole point of upgrading from the lexical fallback."""
    vecs = emb._local_embed([
        "annual recurring revenue for the quarter",
        "quarterly subscription revenue",
        "how to restart the docker container",
    ])
    def cos(a, b):
        return sum(x * y for x, y in zip(a, b))
    assert cos(vecs[0], vecs[1]) > cos(vecs[0], vecs[2])


def test_batch_local_fallback_fills_all(db=None):
    texts = ["save this note", "query the sales table"]
    out = emb.get_embeddings_batch(texts)
    assert len(out) == 2
    assert all(v for v in out)


def test_circuit_breaker_opens_on_api_failure(monkeypatch):
    """A broken API must open the breaker: the next call goes local without
    touching the API again."""
    calls = {"n": 0}

    def _boom(texts, **kwargs):
        calls["n"] += 1
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(emb, "_post_embeddings", _boom)
    monkeypatch.setattr(emb, "_API_FAIL_UNTIL", 0.0)

    # api_key supplied → the API path runs (and fails) → breaker opens
    r1 = emb.get_embedding("first call", use_cache=False, api_key="fake")
    assert r1 is not None and r1.model == "local"
    assert calls["n"] == 1

    r2 = emb.get_embedding("second call", use_cache=False, api_key="fake")
    assert r2 is not None
    assert calls["n"] == 1  # breaker open — API not retried

    emb._API_FAIL_UNTIL = 0.0  # reset for other tests


def test_fallback_flag_off_degrades_to_none(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EMBEDDING_LOCAL_FALLBACK_ENABLED", False)
    res = emb.get_embedding("anything meaningful", use_cache=False)
    assert res is None  # legacy behavior: None → lexical scoring
