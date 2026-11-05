"""Phase 2.5 — tool output & evidence validation (TDD).

Tool execution success ≠ objective satisfied. This module validates returned
STRUCTURE, required fields, provenance, freshness, shape consistency and
cross-source numerical consistency. It NEVER rewrites SQL, NEVER invents
calculations, and never touches the NL2SQL validation gates (which run
upstream, unchanged).

Run: pytest tests/test_evidence_validation.py -q
"""
from datetime import datetime, timedelta, timezone

from app.services.evidence_validation import (
    compare_metric_values,
    validate_delegation_result,
    validate_query_result,
    validate_retrieval,
)


# ── Query results ────────────────────────────────────────────────────────────
def test_valid_query_result_passes():
    r = validate_query_result({
        "success": True, "columns": ["model", "revenue"],
        "rows": [["A", 100], ["B", 200]], "source_id": "ds-1",
    }, required_columns=("model", "revenue"))
    assert r.status == "validated"
    assert r.issues == []


def test_missing_required_column_is_invalid():
    r = validate_query_result({
        "success": True, "columns": ["model"], "rows": [["A"]],
        "source_id": "ds-1",
    }, required_columns=("model", "revenue"))
    assert r.status == "invalid"
    assert any("revenue" in i for i in r.issues)


def test_ragged_rows_are_flagged():
    r = validate_query_result({
        "success": True, "columns": ["a", "b"],
        "rows": [[1, 2], [3]], "source_id": "ds-1",
    })
    assert r.status == "invalid"
    assert any("shape" in i.lower() or "ragged" in i.lower() for i in r.issues)


def test_missing_provenance_is_partial_not_validated():
    r = validate_query_result({
        "success": True, "columns": ["a"], "rows": [[1]],
    })
    assert r.status == "partial"
    assert any("provenance" in i.lower() or "source" in i.lower() for i in r.issues)


def test_empty_result_is_validated_with_empty_note():
    r = validate_query_result({
        "success": True, "columns": ["a"], "rows": [], "source_id": "ds-1",
    })
    assert r.status == "validated"
    assert any("empty" in i.lower() for i in r.issues)


def test_stale_data_is_flagged_as_partial():
    old = datetime.now(timezone.utc) - timedelta(days=60)
    r = validate_query_result({
        "success": True, "columns": ["a"], "rows": [[1]], "source_id": "ds-1",
        "as_of": old.isoformat(),
    }, max_age_days=7)
    assert r.status == "partial"
    assert any("stale" in i.lower() or "freshness" in i.lower() for i in r.issues)


def test_failed_execution_is_invalid():
    r = validate_query_result({"success": False, "error": "boom"})
    assert r.status == "invalid"


# ── Cross-source numerical consistency (Scenario E) ─────────────────────────
def test_metric_mismatch_within_tolerance_passes():
    assert compare_metric_values(120.0, 120.5, tolerance=0.01) == "consistent"


def test_metric_mismatch_beyond_tolerance_contradicts():
    verdict = compare_metric_values(120.0, 90.0, tolerance=0.01)
    assert verdict == "contradiction"
    # Never silently pick a number — the caller must explain the difference.


def test_metric_comparison_handles_none_honestly():
    assert compare_metric_values(None, 90.0) == "incomplete"


# ── RAG retrieval ────────────────────────────────────────────────────────────
def test_retrieval_with_citations_validated():
    r = validate_retrieval([
        {"text": "doc content", "source": "policy.pdf", "score": 0.82},
    ])
    assert r.status == "validated"


def test_retrieval_without_citations_is_partial():
    r = validate_retrieval([{"text": "doc content", "score": 0.2}])
    assert r.status == "partial"
    assert any("citation" in i.lower() or "source" in i.lower() for i in r.issues)


def test_low_relevance_scores_flag_insufficient_coverage():
    r = validate_retrieval([
        {"text": "x", "source": "a.pdf", "score": 0.3},
    ], relevance_threshold=0.6)
    assert r.status == "partial"
    assert any("coverage" in i.lower() or "relevan" in i.lower() for i in r.issues)


def test_empty_retrieval_is_insufficient_not_validated():
    assert validate_retrieval([]).status == "partial"


# ── Specialist delegation results ────────────────────────────────────────────
def test_delegation_success_with_contract_and_evidence_validated():
    r = validate_delegation_result({
        "success": True, "status": "completed", "response": "findings",
        "agent_task_contract": {"objective": "x"},
        "evidence_ids": ["e1"],
    })
    assert r.status == "validated"


def test_delegation_success_without_evidence_is_partial():
    r = validate_delegation_result({
        "success": True, "status": "completed", "response": "findings",
        "agent_task_contract": {"objective": "x"},
    })
    assert r.status == "partial"
    assert any("evidence" in i.lower() for i in r.issues)


def test_failed_execution_is_not_successful_outcome():
    # Distinguish successful EXECUTION from successful BUSINESS OUTCOME.
    r = validate_delegation_result({
        "success": True, "status": "partial", "truncated": True, "response": "half",
        "evidence_ids": ["e1"],
    })
    assert r.status == "partial"


def test_failed_delegation_is_invalid():
    assert validate_delegation_result({"success": False, "error": "x"}).status == "invalid"
