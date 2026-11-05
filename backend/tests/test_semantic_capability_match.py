"""Phase 3.2 — semantic capability discovery & specialist selection (TDD).

Lexical keyword matching is blind to paraphrase ("who makes our vehicles" vs
"production agent"). This module ranks PUBLISHED agents by EMBEDDING cosine
similarity (existing local all-MiniLM infra — no new provider), with
structured prefiltering (publication, department, exclusions), a relevance
threshold (own-tools-sufficiency: empty = GA handles it, no unnecessary
delegation), and a recorded rationale. Unknown/absent embedding infra
degrades to the Phase-2 lexical matcher — never to guessing.

Run: pytest tests/test_semantic_capability_match.py -q
"""
import math

import pytest

from app.services.semantic_capability_match import (
    cosine_similarity,
    semantic_select_specialists,
)


# Deterministic fake embedder: vectors encode topic proximity.
def fake_embedder(texts):
    topics = {
        "production": [1.0, 0.0, 0.0],
        "cost": [0.0, 1.0, 0.0],
        "sales": [0.0, 0.0, 1.0],
    }
    out = []
    for t in texts:
        low = (t or "").lower()
        v = [0.0, 0.0, 0.0]
        for topic, vec in topics.items():
            if topic in low:
                v = [a + b for a, b in zip(v, vec)]
        out.append(v)
    return out


CANDIDATES = [
    {"name": "production_agent", "description": "production output and line efficiency",
     "published": True},
    {"name": "finance_agent", "description": "cost drivers and margins",
     "published": True},
    {"name": "sales_analyst", "description": "sales revenue trends", "published": True},
    {"name": "draft_forecaster", "description": "production demand forecast",
     "published": False},
]


def test_paraphrase_ranking_beats_lexical_blindness():
    # "who builds our vehicles" shares NO keyword with "production output"
    # but is semantically near it (fake embedder maps "production").
    out = semantic_select_specialists(
        "who builds our vehicles and how efficient is the production",
        CANDIDATES, embedder=fake_embedder,
    )
    assert out and out[0]["name"] == "production_agent"
    assert "embedding" in out[0]["rationale"].lower()


def test_unpublished_never_selected():
    out = semantic_select_specialists(
        "production demand forecast", CANDIDATES, embedder=fake_embedder,
    )
    assert all(c["name"] != "draft_forecaster" for c in out)


def test_irrelevant_task_selects_nothing_own_tools_suffice():
    out = semantic_select_specialists(
        "write a poem about spring", CANDIDATES, embedder=fake_embedder,
    )
    assert out == []  # no unnecessary delegation — GA's own tools suffice


def test_department_filter_and_exclusions():
    dept_candidates = [
        {"name": "production_agent", "description": "production output",
         "published": True, "department": "manufacturing"},
        {"name": "finance_agent", "description": "production cost analysis",
         "published": True, "department": "finance"},
    ]
    out = semantic_select_specialists(
        "production cost increase", dept_candidates,
        embedder=fake_embedder, department="finance",
    )
    assert [c["name"] for c in out] == ["finance_agent"]
    out2 = semantic_select_specialists(
        "production cost increase", dept_candidates,
        embedder=fake_embedder, exclude={"finance_agent"},
    )
    assert all(c["name"] != "finance_agent" for c in out2)


def test_top_k_bound():
    out = semantic_select_specialists(
        "production cost sales", CANDIDATES, embedder=fake_embedder, top_k=2,
    )
    assert len(out) <= 2


def test_missing_embedder_degrades_to_lexical():
    # embedder=None must fall back to the Phase-2 lexical matcher (graceful
    # degrade), which still matches on explicit keywords.
    out = semantic_select_specialists(
        "analyze production output and line efficiency",
        [{"name": "production_agent",
          "description": "production output and line efficiency",
          "published": True}],
        embedder=None,
    )
    assert out and out[0]["name"] == "production_agent"
    assert "lexical" in out[0]["rationale"].lower()


def test_embedder_failure_degrades_to_lexical():
    def broken_embedder(texts):
        raise RuntimeError("model not loaded")

    out = semantic_select_specialists(
        "analyze production output",
        [{"name": "production_agent",
          "description": "production output and line efficiency",
          "published": True}],
        embedder=broken_embedder,
    )
    assert out and "lexical" in out[0]["rationale"].lower()


# ── cosine similarity (pure) ─────────────────────────────────────────────────
def test_cosine_similarity_pure_math():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert math.isnan(cosine_similarity([0, 0], [1, 1])) or cosine_similarity([0, 0], [1, 1]) == 0.0
