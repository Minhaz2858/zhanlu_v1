"""Tests for the golden-eval regression runner (2026-08-29, F3).

Covers: payload parsing, gate-verdict math (floor/parity/regression/fail-closed),
champion stats aggregation, judge-endpoint resolution, and the full
``run_golden_suite`` flow with mocked sub-agent + judge (EvalResult persistence,
case counters, timeout fail-closed, regression detection).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
from app.models.agent_test_case import AgentTestCase
from app.models.eval_result import EvalResult
from app.models.llm_model import LlmModel
from app.services.golden_eval_runner import (
    _champion_stats,
    _gate_verdict,
    _parse_case_payload,
    _resolve_judge_endpoint,
    run_golden_suite,
)
from app.services.synexia.quality_eval import QualityEvalResult
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    s.query(EvalResult).delete()
    s.query(AgentTestCase).delete()
    s.query(LlmModel).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _case(db, name="case-a", *, assertions=None, status="active", test_type="unit"):
    c = AgentTestCase(
        agent_app_id="agent-1",
        name=name,
        description=f"Golden case {name}",
        test_type=test_type,
        status=status,
        input_json={"user_message": f"handle {name}"},
        expected_output_json={"expected_accuracy": 0.6},
        expected_behavior="answer the user",
        assertions=assertions or ["all_parts_answered"],
    )
    db.add(c)
    return c


def _champ_row(db, case_name, *, verdict="accept", completeness=0.9, model="champ-1"):
    r = EvalResult(
        conversation_id=f"golden:{case_name}",
        user_message=case_name,
        assistant_text="ok",
        scores=json.dumps({"completeness": completeness, "confidence": 0.8}),
        verdict=verdict,
        model=model,
    )
    db.add(r)
    return r


# ── payload parsing ────────────────────────────────────────────────────────

def test_parse_case_payload_handles_str_dict_none(db):
    c = _case(db, "payload-a")
    c.input_json = None
    c.expected_output_json = None
    assert _parse_case_payload(c) == {"input": {}, "expected": {}}

    c.input_json = '{"user_message": "hi"}'
    c.expected_output_json = '{"expected_accuracy": 0.7}'
    parsed = _parse_case_payload(c)
    assert parsed["input"]["user_message"] == "hi"
    assert parsed["expected"]["expected_accuracy"] == 0.7

    c.input_json = "not-json{{"
    assert _parse_case_payload(c)["input"] == {}


# ── gate verdict math ──────────────────────────────────────────────────────

def test_gate_verdict_below_floor_fails():
    cand = {"n": 4, "pass_rate": 0.5, "mean_completeness": 0.5, "regressed_cases": []}
    assert _gate_verdict(cand, None) == "fail"


def test_gate_verdict_no_champion_passes_when_floor_met():
    cand = {"n": 4, "pass_rate": 0.85, "mean_completeness": 0.8, "regressed_cases": []}
    assert _gate_verdict(cand, None) == "pass"


def test_gate_verdict_warn_when_below_champion_beyond_tolerance():
    cand = {"n": 4, "pass_rate": 0.85, "mean_completeness": 0.8, "regressed_cases": []}
    champ = {"pass_rate": 1.0, "mean_completeness": 0.95}
    assert _gate_verdict(cand, champ) == "warn"


def test_gate_verdict_pass_within_parity():
    cand = {"n": 4, "pass_rate": 0.98, "mean_completeness": 0.94, "regressed_cases": []}
    champ = {"pass_rate": 1.0, "mean_completeness": 0.95}
    assert _gate_verdict(cand, champ) == "pass"


def test_gate_verdict_hard_fail_on_regressed_case():
    cand = {
        "n": 4, "pass_rate": 0.75, "mean_completeness": 0.8, "regressed_cases": [],
    }
    cand["regressed_cases"] = [{"case_name": "x", "champion": "pass", "candidate": "revise"}]
    assert _gate_verdict(cand, None) == "fail"


def test_gate_verdict_empty_run_fails_closed():
    cand = {"n": 0, "pass_rate": 0.0, "mean_completeness": 0.0, "regressed_cases": []}
    assert _gate_verdict(cand, None) == "fail"


# ── champion stats ─────────────────────────────────────────────────────────

def test_champion_stats_aggregates_history(db):
    _champ_row(db, "case-a", verdict="accept", completeness=0.9)
    _champ_row(db, "case-b", verdict="accept", completeness=0.8)
    _champ_row(db, "case-c", verdict="fail", completeness=0.4)
    db.commit()

    stats = _champion_stats(db, "champ-1")
    assert stats["n"] == 3
    assert stats["pass_rate"] == round(2 / 3, 3)
    assert stats["mean_completeness"] == round((0.9 + 0.8 + 0.4) / 3, 3)


def test_champion_stats_empty_returns_none(db):
    assert _champion_stats(db, "champ-1") is None


# ── judge endpoint resolution ──────────────────────────────────────────────

def test_resolve_judge_endpoint_from_row(db):
    db.add(LlmModel(
        name="Champ", model_id="champ-1", provider="custom",
        base_url="https://champ.example/v1", api_key="secret", enabled=True,
    ))
    db.commit()
    ep = _resolve_judge_endpoint(db, "champ-1")
    assert ep is not None
    assert ep.model_id == "champ-1"
    assert ep.base_url == "https://champ.example/v1"
    assert ep.api_key == "secret"


def test_resolve_judge_endpoint_unknown_returns_none(db):
    assert _resolve_judge_endpoint(db, "no-such-model") is None


# ── full suite run (mocked sub-agent + judge) ──────────────────────────────

@pytest.mark.asyncio
async def test_run_golden_suite_persists_and_passes(db, monkeypatch):
    _case(db, "case-a")
    _case(db, "case-b")
    _champ_row(db, "case-a", verdict="accept", completeness=0.9)
    _champ_row(db, "case-b", verdict="accept", completeness=0.9)
    db.commit()

    async def fake_sub_agent(**kwargs):
        return {"success": True, "response": "Handled the request correctly.", "iterations": 2}

    def fake_judge(**kwargs):
        return QualityEvalResult(verdict="accept", completeness_score=0.9, confidence=0.8)

    monkeypatch.setattr(
        "app.services.tool_handlers.delegate_tool._run_sub_agent_inner", fake_sub_agent
    )
    monkeypatch.setattr("app.services.synexia.quality_eval.evaluate_quality", fake_judge)

    report = await run_golden_suite(
        db,
        endpoint=None,  # not touched when the sub-agent is mocked
        model_label="candidate-1",
        champion_label="champ-1",
        seed_if_empty=False,
    )

    assert report["status"] == "pass"
    assert report["candidate"]["n"] == 2
    assert report["candidate"]["pass_rate"] == 1.0
    assert report["champion"]["pass_rate"] == 1.0

    # EvalResult rows persisted under golden:<case> with candidate label.
    rows = db.query(EvalResult).filter(EvalResult.model == "candidate-1").all()
    assert len(rows) == 2
    assert {r.conversation_id for r in rows} == {"golden:case-a", "golden:case-b"}
    assert all(r.verdict == "accept" for r in rows)

    # Case counters bumped.
    cases = db.query(AgentTestCase).order_by(AgentTestCase.name).all()
    assert all(c.run_count == 1 for c in cases)
    assert all(c.pass_count == 1 for c in cases)
    assert all(c.last_result == "pass" for c in cases)


@pytest.mark.asyncio
async def test_run_golden_suite_fail_closed_on_timeout(db, monkeypatch):
    _case(db, "case-timeout")
    db.commit()

    async def hang(**kwargs):
        await asyncio.sleep(30)  # longer than the gate timeout

    monkeypatch.setattr(
        "app.services.tool_handlers.delegate_tool._run_sub_agent_inner", hang
    )
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "EVAL_GATE_CASE_TIMEOUT_S", 0.05)

    report = await run_golden_suite(
        db,
        endpoint=None,
        model_label="candidate-1",
        champion_label=None,
        seed_if_empty=False,
    )

    assert report["cases"][0]["timed_out"] is True
    assert report["cases"][0]["pass"] is False
    assert report["status"] == "fail"  # fail-closed


@pytest.mark.asyncio
async def test_run_golden_suite_detects_regression(db, monkeypatch):
    _case(db, "case-reg")
    _champ_row(db, "case-reg", verdict="accept", completeness=0.9)  # champion passed
    db.commit()

    async def failing_sub_agent(**kwargs):
        return {"success": False, "error": "tool failed", "response": "Sorry, I could not complete the task."}

    def lenient_judge(**kwargs):
        # Judge accepts even the failed run — the run-level success flag fails it.
        return QualityEvalResult(verdict="accept", completeness_score=0.9, confidence=0.8)

    monkeypatch.setattr(
        "app.services.tool_handlers.delegate_tool._run_sub_agent_inner", failing_sub_agent
    )
    monkeypatch.setattr("app.services.synexia.quality_eval.evaluate_quality", lenient_judge)

    report = await run_golden_suite(
        db,
        endpoint=None,
        model_label="candidate-1",
        champion_label="champ-1",
        seed_if_empty=False,
    )

    assert report["status"] == "fail"
    assert len(report["regressed_cases"]) == 1
    assert report["regressed_cases"][0]["case_name"] == "case-reg"


@pytest.mark.asyncio
async def test_run_golden_suite_no_cases_fails(db):
    report = await run_golden_suite(
        db,
        endpoint=None,
        model_label="candidate-1",
        champion_label=None,
        seed_if_empty=False,
    )
    assert report["status"] == "fail"
    assert report["reason"] == "no_golden_cases"
