"""Tests for the LLM planning router (Task 2 of P1 plan)."""

from __future__ import annotations

import pytest

from app.config import settings
import app.services.planning_trigger as pt


# --- Heuristic mode (default) — existing behavior preserved ----------------


def test_heuristic_multistep_english_triggers():
    t = pt.should_trigger_planning("Create a report and then email it to the manager")
    assert t.should_plan is True
    assert t.source == "heuristic"


def test_heuristic_simple_question_does_not_trigger():
    t = pt.should_trigger_planning("What is 2+2?")
    assert t.should_plan is False
    # Source may be "heuristic" (score below threshold) or
    # "heuristic-bypass" (simple-conversation bypass) — both are valid.
    assert t.source in ("heuristic", "heuristic-bypass")


def test_heuristic_empty_message_does_not_trigger():
    t = pt.should_trigger_planning("")
    assert t.should_plan is False
    assert t.confidence == 0.0


# --- llm mode ---------------------------------------------------------------


def test_llm_mode_classifies_chinese_multistep(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ROUTER_MODE", "llm")
    monkeypatch.setattr(
        pt, "_classify_with_llm",
        lambda msg, llm_callable=None: {"should_plan": True, "confidence": 0.9, "rationale": "multistep"},
    )
    t = pt.should_trigger_planning("先创建一个报告，然后把它发给经理")
    assert t.should_plan is True
    assert t.source == "llm"
    assert t.confidence == 0.9


def test_llm_mode_falls_back_to_heuristic_when_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ROUTER_MODE", "llm")
    # Simulate LLM service down by injecting a callable that returns None
    monkeypatch.setattr(pt, "_classify_with_llm", lambda msg, llm_callable=None: None)
    t = pt.should_trigger_planning("Create a report and then email it")
    assert t.should_plan is True  # heuristic catches it
    assert t.source == "heuristic"


def test_llm_mode_falls_back_on_low_confidence(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ROUTER_MODE", "llm")
    monkeypatch.setattr(
        pt, "_classify_with_llm",
        lambda msg, llm_callable=None: {"should_plan": True, "confidence": 0.3, "rationale": "unsure"},
    )
    # "Create a report and then email it" → heuristic score 0.8 (1 connective + 2 verbs)
    t = pt.should_trigger_planning("Create a report and then email it")
    # LLM said yes but with low confidence → fall back to heuristic
    assert t.source == "heuristic"
    assert t.should_plan is True


# --- hybrid mode ------------------------------------------------------------


def test_hybrid_skips_llm_when_heuristic_clear(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ROUTER_MODE", "hybrid")
    called = []
    def _llm(msg, llm_callable=None):
        called.append(1)
        return {"should_plan": True, "confidence": 0.95, "rationale": ""}
    monkeypatch.setattr(pt, "_classify_with_llm", _llm)

    # "plan, then create, then send" → high heuristic score, no LLM call.
    t = pt.should_trigger_planning("Create a plan, then build a workflow, then deploy it")
    assert t.source == "hybrid-heuristic"
    assert called == []  # LLM was not consulted — heuristic was decisive.


def test_hybrid_invokes_llm_in_gray_band(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ROUTER_MODE", "hybrid")
    monkeypatch.setattr(
        pt, "_classify_with_llm",
        lambda msg, llm_callable=None: {"should_plan": True, "confidence": 0.9, "rationale": "multistep"},
    )
    # "Plan a report" → 1 plan keyword (0.4) → score in the gray band [0.2, 0.6].
    # LLM should be consulted and its verdict returned.
    t = pt.should_trigger_planning("Plan a report")
    assert t.source == "hybrid-llm"
    assert t.should_plan is True


def test_hybrid_uses_heuristic_when_llm_low_confidence(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_ROUTER_MODE", "hybrid")
    monkeypatch.setattr(
        pt, "_classify_with_llm",
        lambda msg, llm_callable=None: {"should_plan": True, "confidence": 0.4, "rationale": "unsure"},
    )
    t = pt.should_trigger_planning("Plan to create a report, then send it")
    # LLM was consulted (in gray band) but confidence below threshold → heuristic
    assert t.source == "hybrid-heuristic"


# --- Classifier robustness --------------------------------------------------


def test_classify_with_llm_handles_malformed_response():
    out = pt._classify_with_llm("anything", llm_callable=lambda _p: {"checks": "nope"})
    assert out is None


def test_classify_with_llm_normalizes_types():
    out = pt._classify_with_llm("anything", llm_callable=lambda _p: {"should_plan": 1, "confidence": "0.8", "rationale": 42})
    assert out is not None
    assert out["should_plan"] is True
    assert out["confidence"] == 0.8
    assert out["rationale"] == "42"


def test_classify_with_llm_swallows_exceptions():
    def boom(_p):
        raise RuntimeError("LLM down")
    out = pt._classify_with_llm("anything", llm_callable=boom)
    assert out is None


def test_classify_with_llm_requires_both_keys():
    out = pt._classify_with_llm("anything", llm_callable=lambda _p: {"should_plan": True})
    assert out is None
    out = pt._classify_with_llm("anything", llm_callable=lambda _p: {"confidence": 0.5})
    assert out is None


def test_default_mode_is_heuristic():
    assert settings.PLANNING_ROUTER_MODE == "heuristic"
