"""Tests for the eval harness."""

import asyncio
import json
import os
import tempfile

import pytest

from app.services.synexia.eval_harness import (
    BUILTIN_SCENARIOS,
    EvalRunner,
    HarnessReport,
    ScenarioResult,
    grade_artifact_created,
    grade_completeness_at_least,
    grade_confidence_at_least,
    grade_contains,
    grade_not_contains,
    grade_quality_eval_passed,
    grade_reflexion_verdict,
    load_all_scenarios,
    load_scenarios_from_file,
    run_harness,
)


def test_grade_contains_positive_and_negative():
    assert grade_contains({"assistant_content": "Sales_Report"}, "Sales_Report").passed
    assert not grade_contains({"assistant_content": "Hello"}, "Sales_Report").passed


def test_grade_not_contains_inverse():
    assert grade_not_contains({"assistant_content": "Hello"}, "404").passed
    assert not grade_not_contains(
        {"assistant_content": "Failed to load artifact: HTTP 404"}, "404"
    ).passed


def test_grade_artifact_created():
    out_pass = {"artifact_ids": ["abc"]}
    out_fail = {"artifact_ids": []}
    assert grade_artifact_created(out_pass, True).passed
    assert not grade_artifact_created(out_fail, True).passed
    # When expected=False, an empty list passes.
    assert grade_artifact_created(out_fail, False).passed


def test_grade_confidence_at_least():
    assert grade_confidence_at_least({"confidence": 0.7}, 0.5).passed
    assert not grade_confidence_at_least({"confidence": 0.3}, 0.5).passed


def test_eval_runner_passes_good_scenarios():
    async def runner(scenario):
        return {
            "assistant_content": "Here is your Sales_Report.",
            "artifact_ids": ["abc"],
            "confidence": 0.8,
        }

    scenarios = [
        {
            "name": "good",
            "user_message": "make a sales report for me",
            "expect": {
                "contains": ["Sales_Report"],
                "artifact_created": True,
                "confidence_at_least": 0.5,
            },
        }
    ]
    report = asyncio.run(EvalRunner(run_fn=runner, scenarios=scenarios).run())
    assert report.total == 1
    # Find the "good" scenario.
    good = next(s for s in report.scenarios if s.name == "good")
    assert good.passed is True


def test_eval_runner_fails_when_assistant_text_contains_404():
    async def runner(scenario):
        return {
            "assistant_content": "Failed to load artifact: HTTP 404",
            "artifact_ids": [],
            "confidence": 0.0,
        }

    report = asyncio.run(run_harness(runner))
    # The built-in scenarios that require not_contains=["Failed to load artifact", "HTTP 404"]
    # must all fail.  The "confidence_above_threshold" scenario should
    # also fail.
    assert report.failed > 0


def test_load_scenarios_from_file_returns_empty_for_missing_path():
    assert load_scenarios_from_file("/nope/does/not/exist.json") == []


def test_load_scenarios_from_file_reads_user_file():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        json.dump(
            [
                {
                    "name": "user_scenario",
                    "user_message": "hi",
                    "expect": {"contains": ["hi"]},
                }
            ],
            f,
        )
        path = f.name
    try:
        out = load_scenarios_from_file(path)
        assert len(out) == 1
        assert out[0]["name"] == "user_scenario"
    finally:
        os.unlink(path)


def test_load_all_scenarios_merges_builtins_and_user_file():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        json.dump(
            [{"name": "x", "user_message": "x", "expect": {}}],
            f,
        )
        path = f.name
    try:
        out = load_all_scenarios(user_file=path)
        names = {s["name"] for s in out}
        assert "x" in names
        # The four built-ins are also there.
        assert {"sales_report_minimal", "db_overview_no_404",
                "clarify_batch_resolves", "confidence_above_threshold"}.issubset(names)
    finally:
        os.unlink(path)


def test_builtin_scenarios_cover_user_pain_points():
    """Regression guard: every built-in scenario must include at least
    one assertion that addresses a known production bug."""
    names = {s["name"] for s in BUILTIN_SCENARIOS}
    expected = {
        "sales_report_minimal",
        "db_overview_no_404",
        "clarify_batch_resolves",
        "confidence_above_threshold",
    }
    assert expected.issubset(names)


# ── QUALITY_EVAL graders (Tier 2 — Approach C) ───────────────────────────


def _qe_output(verdict="accept", completeness=0.9, is_ok=True):
    return {"quality_eval": {
        "verdict": verdict,
        "completeness_score": completeness,
        "confidence": 0.8,
        "issues": [],
        "suggestions": [],
        "iterations": 0,
        "is_ok": is_ok,
    }}


def test_grade_quality_eval_passed_accept():
    out = _qe_output(verdict="accept", is_ok=True)
    assert grade_quality_eval_passed(out, True).passed


def test_grade_quality_eval_failed_when_revise():
    out = _qe_output(verdict="revise", is_ok=False)
    assert not grade_quality_eval_passed(out, True).passed


def test_grade_quality_eval_absent_counts_as_pass():
    """No quality_eval key (QUALITY_EVAL disabled) → treated as pass."""
    assert grade_quality_eval_passed({"assistant_content": "hi"}, True).passed


def test_grade_quality_eval_absent_fails_when_hold_expected():
    assert not grade_quality_eval_passed({"assistant_content": "hi"}, False).passed


def test_grade_completeness_at_least():
    out = _qe_output(completeness=0.8)
    assert grade_completeness_at_least(out, 0.5).passed
    assert not grade_completeness_at_least(out, 0.9).passed


def test_grade_completeness_absent_fails():
    """No quality_eval → completeness check fails (can't verify)."""
    g = grade_completeness_at_least({"assistant_content": "hi"}, 0.5)
    assert not g.passed


def test_grade_reflexion_verdict():
    out = _qe_output(verdict="accept")
    assert grade_reflexion_verdict(out, "accept").passed
    assert not grade_reflexion_verdict(out, "revise").passed


def test_eval_runner_dispatches_quality_eval_graders():
    async def runner(scenario):
        return _qe_output(verdict="accept", completeness=0.85, is_ok=True)

    scenarios = [
        {
            "name": "quality_ok",
            "user_message": "make a sales report",
            "expect": {
                "quality_eval_passed": True,
                "completeness_at_least": 0.5,
                "reflexion_verdict": "accept",
            },
        }
    ]
    report = asyncio.run(EvalRunner(run_fn=runner, scenarios=scenarios).run())
    sc = next(s for s in report.scenarios if s.name == "quality_ok")
    assert sc.passed is True


def test_builtin_scenarios_include_quality_eval_scenario():
    """A quality-focused built-in scenario must exist."""
    names = {s["name"] for s in BUILTIN_SCENARIOS}
    assert any("quality" in n for n in names)
