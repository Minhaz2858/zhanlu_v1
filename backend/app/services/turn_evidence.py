"""Direct-execution evidence capture (root-cause audit D2, 2026-10-31).

The evidence pipeline used to be delegation-only: ``provide_evidence`` →
EvidenceRegistry → child ``output_json.evidence_ids`` → tree evidence_count.
A direct-execution General Assistant turn (its own governed NL2SQL tools)
produced ZERO evidence even when its queries succeeded — the execution panel
reported "Evidence: 0" under a fully numerical answer.

Fix: at turn end, successful DATA-tool results are stored as REAL
EvidenceItems in the existing shared registry (provenance: tool, row count,
columns — compact findings, never raw row dumps) and their ids are referenced
from the GA turn's ``AgentInvocation.output_json.evidence_ids``. The ids are
resolvable via the existing ``GET /executions/evidence`` endpoint.

Honesty rules:
  * only successful data-returning tools produce evidence;
  * items are ``unvalidated`` — a stored reference is NOT a verification;
  * failed tools are listed by the verify gate instead (D3), never stored.
"""
from __future__ import annotations

import uuid

# Tools whose SUCCESSFUL results constitute query evidence (governed NL2SQL
# family). Context tools (describe_schema, think, todo, …) and artifact
# builders intentionally excluded.
DATA_EVIDENCE_TOOLS = {
    "query_database", "execute_query", "answer_from_database",
    "ask_data_agent", "universal_query", "universal_kpi",
    "universal_trend", "universal_forecast",
}

_SUCCESS_STATUSES = {"completed", "success", "done", "ok", ""}


def _status(tc) -> str:
    return str((tc or {}).get("status") or "").lower()


def _is_successful_data_call(tc) -> bool:
    # Whitelist semantics: a data call is evidence ONLY when it actually
    # completed — paused (awaiting_decision_summary), blocked, and failed
    # states never produce evidence.
    return (
        (tc or {}).get("name") in DATA_EVIDENCE_TOOLS
        and _status(tc) in _SUCCESS_STATUSES
    )


def _compact_findings(tc) -> dict:
    """Compact, bounded findings — provenance, not a raw-row dump."""
    results = (tc or {}).get("results") or {}
    findings: dict = {"tool": (tc or {}).get("name")}
    row_count = results.get("row_count")
    if row_count is None and isinstance(results.get("rows"), list):
        row_count = len(results["rows"])
    if row_count is not None:
        findings["row_count"] = row_count
    cols = results.get("columns")
    if isinstance(cols, list) and cols:
        findings["columns"] = [str(c) for c in cols[:12]]
    answer = results.get("answer") or results.get("summary")
    if isinstance(answer, str) and answer.strip():
        findings["answer_excerpt"] = answer.strip()[:300]
    source = results.get("data_source_id") or results.get("source_id")
    if source:
        findings["source_id"] = str(source)[:80]
    return findings


def _validate_results(results) -> tuple[str | None, list[str]]:
    """C-1 (enterprise reliability audit, 2026-11-01): verdict a successful
    data-tool result with the Phase-2.5 validators (pure functions — no LLM).

    Returns (validation_status | None, limitations). ``None`` means
    validation did not run (flag off / validator error) and the item keeps
    the honest "unvalidated" limitation. Never raises.
    """
    try:
        from app.config import settings as _settings

        if not getattr(_settings, "EVIDENCE_VALIDATION_ENABLED", True):
            return None, []
        from app.services.evidence_validation import (
            INVALID,
            PARTIAL,
            VALIDATED,
            validate_query_result,
        )

        # The validator reads ``results["success"]`` to separate execution
        # failure from structure problems — but many completed tool results
        # (compact findings, ask_data_agent payloads, legacy shapes) omit
        # the key. Derive it from the CAPTURED CALL STATUS at the call site:
        # record_turn_evidence only routes status=="completed" calls here,
        # so an absent key means "completed, success not asserted" — NOT a
        # failed execution. Only an explicit ``success: False`` (or a
        # failed call, which never reaches this function) is an execution
        # failure. (Root fix for test_turn_evidence invalid-vs-unvalidated,
        # enterprise reliability milestone 2026-11-01.)
        _view = dict(results or {})
        _view.setdefault("success", True)
        verdict = validate_query_result(_view)
        if verdict.status == VALIDATED:
            return "validated", []
        limits = [
            f"validation {verdict.status}: {issue}"
            for issue in verdict.issues[:5]
        ]
        if verdict.status == PARTIAL:
            return PARTIAL, limits
        return INVALID, limits
    except Exception:  # noqa: BLE001 — validation must never break capture
        return None, []


def record_turn_evidence(
    conversation_id: str,
    tool_calls,
    producer_agent: str = "general_assistant",
    registry=None,
) -> list[str]:
    """Store successful data-tool results as shared evidence.

    Returns the stored evidence ids (real registry references, resolvable via
    ``GET /executions/evidence``). Best-effort by design — callers must never
    let evidence capture break the turn.
    """
    if not conversation_id or not tool_calls:
        return []
    try:
        from app.services.agent_collaboration.shared_evidence import (
            EvidenceItem,
            get_registry,
        )

        reg = registry or get_registry()
        ids: list[str] = []
        for tc in tool_calls:
            if not _is_successful_data_call(tc):
                continue
            _results = (tc or {}).get("results") or {}
            _verdict, _v_limits = _validate_results(_results)
            _limitations = (
                _v_limits
                if _verdict is not None
                else ["unvalidated direct-execution tool result"]
            )
            item = EvidenceItem(
                evidence_id=f"ev_{uuid.uuid4().hex[:16]}",
                producer_agent=producer_agent,
                query_provenance={
                    "tool": (tc or {}).get("name"),
                    "origin": "direct_execution",
                },
                findings=_compact_findings(tc),
                limitations=_limitations,
                validation_status=_verdict or "unvalidated",
            )
            ids.append(reg.store(conversation_id, item))
        return ids
    except Exception:  # noqa: BLE001 — evidence capture must never break a turn
        return []
