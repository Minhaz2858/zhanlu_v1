"""Root-cause audit D2/D3 — direct-execution evidence + verify-gate verdict.

Deterministic; the evidence registry is the in-process store (no Redis).
"""
import pytest

from app.services.agent_collaboration.shared_evidence import _InProcessStore
from app.services.turn_evidence import (
    DATA_EVIDENCE_TOOLS,
    record_turn_evidence,
)
from app.routers.agents import _verification_verdict


@pytest.fixture()
def registry():
    return _InProcessStore()


def _tc(name, status="completed", results=None):
    return {"name": name, "status": status, "results": results or {}}


# ── D2: direct-execution results become real, resolvable evidence ───────────
def test_successful_data_tools_store_evidence(registry):
    calls = [
        _tc("query_database", results={"row_count": 12, "columns": ["revenue"],
                                       "rows": [{"revenue": 1}] * 12}),
        _tc("ask_data_agent", results={"answer": "…", "row_count": 3}),
        _tc("read_file", status="failed", results={"error": "resource not found"}),
        _tc("think"),  # non-data tool → no evidence
    ]
    ids = record_turn_evidence("conv-1", calls, registry=registry)
    assert len(ids) == 2
    items = {i.evidence_id: i for i in registry.list_items("conv-1")}
    assert set(ids) == set(items)
    ev = items[ids[0]]
    assert ev.producer_agent == "general_assistant"
    assert ev.findings.get("tool") in DATA_EVIDENCE_TOOLS
    assert ev.findings.get("row_count") == 12
    # Compact findings: raw rows must NOT be dumped into the registry.
    assert "rows" not in ev.findings
    # C-1 (enterprise reliability milestone, 2026-11-01): captured evidence
    # now carries an honest validation verdict from the Phase-2.5 validators
    # (was the constant "unvalidated"). This fixture has no provenance
    # fields, so the honest verdict is "partial" — never silently
    # "validated".
    assert ev.validation_status == "partial"


def test_failed_or_blocked_data_tools_store_nothing(registry):
    calls = [
        _tc("execute_query", status="failed", results={"error": "timeout"}),
        _tc("execute_query", status="blocked"),
        _tc("universal_query", status="awaiting_decision_summary"),
    ]
    assert record_turn_evidence("conv-1", calls, registry=registry) == []
    assert registry.list_items("conv-1") == []


def test_no_data_tools_no_evidence(registry):
    assert record_turn_evidence("conv-1", [_tc("think"), None], registry=registry) == []
    assert record_turn_evidence("conv-1", [], registry=registry) == []


# ── D3: verification verdict = failures AND no data grounding ───────────────
def test_verdict_all_success():
    failed, grounded, steps = _verification_verdict(
        [_tc("query_database"), _tc("think")])
    assert failed is False and grounded is True and steps == []


def test_verdict_failed_tool_with_grounding_is_not_verify_failed():
    # THE audited case: read_file failed, two queries succeeded → the answer
    # IS grounded; verification must not claim overall failure.
    failed, grounded, steps = _verification_verdict([
        _tc("read_file", status="failed"),
        _tc("query_database"),
        _tc("query_database"),
    ])
    assert failed is True
    assert grounded is True
    assert steps == ["read_file"]


def test_verdict_failure_without_grounding_fails():
    failed, grounded, steps = _verification_verdict([
        _tc("read_file", status="failed"),
        _tc("create_artifact", status="failed"),
    ])
    assert failed is True and grounded is False


def test_verdict_empty_and_non_list_inputs():
    assert _verification_verdict([]) == (False, False, [])
    assert _verification_verdict(None) == (False, False, [])
