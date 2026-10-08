"""Tests for the QUALITY_EVAL module — combined completeness + reflexion
critique with a bounded corrective re-generation loop.

The llm_call stubs mirror the real ``call_llm`` signature
``(prompt, messages, **kwargs) -> {"response": str}`` so the FSM can inject
the real function in production and tests inject deterministic stubs.
"""

import json

import pytest

from app.services.synexia.quality_eval import (
    QualityEvalResult,
    evaluate_quality,
    validate_selected_skill_payload,
)


# ── QualityEvalResult dataclass ───────────────────────────────────────────


def test_quality_eval_result_defaults():
    r = QualityEvalResult()
    assert r.verdict == "accept"
    assert r.completeness_score == 0.0
    assert r.confidence == 0.0
    assert r.issues == []
    assert r.suggestions == []
    assert r.iterations == 0
    assert r.final_text == ""


def test_quality_eval_result_is_ok_accept():
    r = QualityEvalResult(verdict="accept", completeness_score=0.8)
    assert r.is_ok is True


def test_quality_eval_result_is_ok_revise():
    r = QualityEvalResult(verdict="revise", completeness_score=0.8)
    assert r.is_ok is False


def test_quality_eval_result_is_ok_low_completeness():
    r = QualityEvalResult(verdict="accept", completeness_score=0.4)
    assert r.is_ok is False


def test_quality_eval_result_to_dict_roundtrip():
    r = QualityEvalResult(
        verdict="revise",
        completeness_score=0.3,
        confidence=0.4,
        issues=["missing summary"],
        suggestions=["add TL;DR"],
        iterations=1,
        final_text="revised",
    )
    d = r.to_dict()
    assert d["verdict"] == "revise"
    assert d["completeness_score"] == pytest.approx(0.3)
    assert d["iterations"] == 1
    assert d["is_ok"] is False
    assert "missing summary" in d["issues"]


# ── evaluate_quality ──────────────────────────────────────────────────────


def _llm_returning(payload: dict):
    """A sync llm_call stub returning a JSON ``response`` string."""

    def _stub(prompt, messages, **kwargs):
        return {"response": json.dumps(payload)}

    return _stub


def test_evaluate_quality_accept():
    out = evaluate_quality(
        user_message="make a sales report",
        assistant_text="Here is the sales report with Q2 figures.",
        task_spec={"acceptance_criteria": ["include revenue figures"]},
        llm_call=_llm_returning({
            "verdict": "accept",
            "completeness_score": 0.85,
            "confidence": 0.9,
            "issues": [],
            "suggestions": [],
        }),
    )
    assert out.verdict == "accept"
    assert out.completeness_score == pytest.approx(0.85)
    assert out.is_ok is True


def test_evaluate_quality_revise():
    out = evaluate_quality(
        user_message="make a sales report",
        assistant_text="Here is the report.",
        task_spec={"acceptance_criteria": ["include revenue figures"]},
        llm_call=_llm_returning({
            "verdict": "revise",
            "completeness_score": 0.4,
            "confidence": 0.45,
            "issues": ["revenue figures missing"],
            "suggestions": ["add a revenue table"],
        }),
    )
    assert out.verdict == "revise"
    assert out.is_ok is False
    assert "revenue figures missing" in out.issues


def test_evaluate_quality_reject():
    out = evaluate_quality(
        user_message="summarize the data",
        assistant_text="I cannot do that.",
        task_spec={},
        llm_call=_llm_returning({
            "verdict": "reject",
            "completeness_score": 0.1,
            "confidence": 0.1,
            "issues": ["refusal"],
            "suggestions": [],
        }),
    )
    assert out.verdict == "reject"
    assert out.is_ok is False


def test_evaluate_quality_no_llm_fallback_clean():
    out = evaluate_quality(
        user_message="make a report",
        assistant_text="Here is the report you asked for.",
        task_spec={},
        llm_call=None,
    )
    assert out.verdict == "accept"
    assert out.raw == "heuristic-only"


def test_evaluate_quality_no_llm_fallback_failure_markers():
    out = evaluate_quality(
        user_message="make a report",
        assistant_text="Failed to load artifact: HTTP 404",
        task_spec={},
        llm_call=None,
    )
    assert out.verdict == "revise"


def test_evaluate_quality_llm_raises_fallback():
    def boom(prompt, messages, **kwargs):
        raise RuntimeError("LLM down")

    out = evaluate_quality(
        user_message="make a report",
        assistant_text="Here is the report.",
        task_spec={},
        llm_call=boom,
    )
    # Never raises — heuristic fallback.
    assert out.verdict in ("accept", "revise", "reject")
    assert out.raw == "heuristic-only"


def test_evaluate_quality_malformed_json_fallback():
    def garbage(prompt, messages, **kwargs):
        return {"response": "this is not json at all"}

    out = evaluate_quality(
        user_message="x",
        assistant_text="Here is the report.",
        task_spec={},
        llm_call=garbage,
    )
    assert out.raw == "heuristic-only"


def test_evaluate_quality_clamps_scores():
    out = evaluate_quality(
        user_message="x",
        assistant_text="y",
        task_spec={},
        llm_call=_llm_returning({
            "verdict": "accept",
            "completeness_score": 99.0,
            "confidence": -5.0,
        }),
    )
    assert 0.0 <= out.completeness_score <= 1.0
    assert 0.0 <= out.confidence <= 1.0


def test_validate_selected_skill_payload_flags_missing_required_elements():
    out = validate_selected_skill_payload(
        skill_name="weekly-sales-report",
        skill_body="# Weekly Sales Report\n\n## Executive Summary\n## KPI Section\n## Recommendations\n## Chart",
        artifact_type="docx",
        payload={"title": "Weekly Sales", "summary": "Done."},
    )
    assert out["verdict"] == "revise"
    assert out["is_ok"] is False
    assert "kpis" in out["missing_elements"]
    assert "recommendations" in out["missing_elements"]


def test_validate_selected_skill_payload_accepts_matching_payload():
    out = validate_selected_skill_payload(
        skill_name="weekly-sales-report",
        skill_body="# Weekly Sales Report\n\n## Executive Summary\n## KPI Section\n## Recommendations\n## Chart",
        artifact_type="docx",
        payload={
            "title": "Weekly Sales",
            "summary": "Done.",
            "methodology": "Data source and scope.",
            "kpis": [{"label": "Revenue", "value": "10"}],
            "chart": {"type": "bar"},
            "recommendations": [{"icon": "arrow-right", "text": "Act"}],
            "insights": [{"icon": "info", "text": "Insight"}],
            "sections": [{"title": "Body"}],
        },
    )
    assert out["missing_elements"] == []
    assert out["is_ok"] is True
