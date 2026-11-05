"""Phase 2.3 — reviewer emits the structured gap fields the decision model
consumes (unsupported_claims / contradictions / failed_acceptance_criteria).

`evaluate_reviewer_findings` (Phase 1) reads these keys, but no producer
emitted them. The reviewer now carries them additively — with DETERMINISTIC
derivation from the seven dimensions so even a minimal LLM payload yields
structured gaps. Bounded loop unchanged.

Run: pytest tests/test_reviewer_findings_structured.py -q
"""
from unittest.mock import patch

from app.services.agent_collaboration.reviewer import (
    DIMENSIONS,
    REVIEW_SCHEMA,
    ReviewReport,
    review_findings,
)


SCHEMA_TEXT = str(REVIEW_SCHEMA)


def test_schema_declares_structured_gap_fields():
    for key in ("unsupported_claims", "contradictions", "failed_acceptance_criteria"):
        assert key in SCHEMA_TEXT, key


def test_from_dict_derives_contradictions_from_dimension_details():
    report = ReviewReport.from_dict({
        "dimensions": {
            "contradictory_findings": {
                "status": "fail",
                "detail": "Table A reports revenue 120, Table B reports 90",
            },
        },
        "overall": "needs_revision",
        "answered": True,
    })
    d = report.to_dict()
    assert d["contradictions"] == ["Table A reports revenue 120, Table B reports 90"]


def test_from_dict_derives_unsupported_claims_from_dimension_details():
    report = ReviewReport.from_dict({
        "dimensions": {
            "unsupported_conclusions": {
                "status": "warn",
                "detail": "profit margin 34% asserted without cost evidence",
            },
        },
        "overall": "needs_revision",
        "answered": True,
    })
    d = report.to_dict()
    assert d["unsupported_claims"] == ["profit margin 34% asserted without cost evidence"]


def test_explicit_lists_win_over_derivation():
    explicit = ["LLM-named claim"]
    report = ReviewReport.from_dict({
        "dimensions": {
            "unsupported_conclusions": {"status": "warn", "detail": "dim detail"},
        },
        "overall": "needs_revision",
        "answered": True,
        "unsupported_claims": explicit,
    })
    assert report.to_dict()["unsupported_claims"] == explicit


def test_ok_dimensions_produce_no_gaps():
    report = ReviewReport.from_dict({
        "dimensions": {
            "contradictory_findings": {"status": "ok", "detail": "consistent"},
            "unsupported_conclusions": {"status": "ok", "detail": "grounded"},
        },
        "overall": "approved",
        "answered": True,
    })
    d = report.to_dict()
    assert d["contradictions"] == []
    assert d["unsupported_claims"] == []


def test_all_seven_dimensions_still_required_in_output():
    d = ReviewReport.from_dict({}).to_dict()
    assert set(d["dimensions"].keys()) == set(DIMENSIONS)


def test_conservative_fallback_carries_empty_gap_lists():
    d = ReviewReport.conservative("reviewer error").to_dict()
    assert d["unsupported_claims"] == []
    assert d["contradictions"] == []
    assert d["failed_acceptance_criteria"] == []


def test_review_findings_passes_acceptance_criteria_into_prompt():
    captured = {}

    def fake_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        return {
            "dimensions": {n: {"status": "ok", "detail": ""} for n in DIMENSIONS},
            "overall": "approved",
            "answered": True,
            "issues": [],
            "needs_evidence": [],
            "failed_acceptance_criteria": ["per-model breakdown missing"],
        }

    with patch(
        "app.services.agent_collaboration.reviewer.chat_completion_json_sync",
        side_effect=fake_llm,
    ):
        report = review_findings(
            "Calculate profitability by model", "answer",
            acceptance_criteria=["per-model breakdown present"],
        )
    assert "per-model breakdown present" in captured["prompt"]
    assert report.to_dict()["failed_acceptance_criteria"] == ["per-model breakdown missing"]


def test_review_findings_never_raises_on_llm_failure():
    with patch(
        "app.services.agent_collaboration.reviewer.chat_completion_json_sync",
        side_effect=RuntimeError("provider down"),
    ):
        report = review_findings("q", "a")
    assert report.overall == "needs_revision"  # conservative, never ok


def test_backward_compat_keys_still_present():
    report = ReviewReport.from_dict({
        "dimensions": {}, "overall": "approved", "answered": True,
        "issues": ["x"], "needs_evidence": ["y"],
    })
    d = report.to_dict()
    for key in ("dimensions", "overall", "answered", "issues", "needs_evidence"):
        assert key in d
