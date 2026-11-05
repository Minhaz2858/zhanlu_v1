"""Tool output & evidence validation (Phase 2.5, 2026-10-31).

ROOT CAUSE ADDRESSED
--------------------
Tool execution success was treated as objective satisfaction. Nothing checked
the RETURNED data: structure, required fields, provenance, freshness, shape
consistency, cross-source numerical consistency, retrieval coverage, or the
specialist's task contract. This module adds those checks as pure functions.

Boundaries:
- NL2SQL validation/execution gates (validator, execution gate, source
  verification) are NOT touched — they run upstream and stay authoritative.
- This module NEVER rewrites SQL, NEVER invents calculations, NEVER fills
  gaps — it only VERDICTS: ``validated`` | ``partial`` | ``invalid`` with
  concrete issues the decision model can act on.
- An empty result is still a valid execution (validated, with a note).
- Stale/insufficient-coverage data is PARTIAL (usable with disclosed
  limitations), not invalid — only structural breakage or failed execution
  is invalid.

Issue severities: HARD (→ invalid), soft (→ partial), note (no effect).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

VALIDATED = "validated"
PARTIAL = "partial"
INVALID = "invalid"


@dataclass
class EvidenceValidation:
    status: str                      # validated | partial | invalid
    issues: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"status": self.status, "issues": self.issues, "checks": self.checks}


def _finish(
    hard: list[str],
    soft: list[str],
    notes: list[str],
    checks: dict[str, bool],
) -> EvidenceValidation:
    issues = hard + soft + notes
    if hard:
        return EvidenceValidation(INVALID, issues, checks)
    if soft:
        return EvidenceValidation(PARTIAL, issues, checks)
    return EvidenceValidation(VALIDATED, issues, checks)


def validate_query_result(
    result: dict,
    *,
    required_columns: tuple[str, ...] = (),
    max_age_days: float | None = None,
) -> EvidenceValidation:
    """Validate a data-tool result dict (rows/columns shape, required columns,
    provenance, freshness). Empty rows remain VALID (with a note)."""
    if not isinstance(result, dict):
        return _finish(["result is not a dict"], [], [], {})
    if not result.get("success"):
        return _finish(
            [f"execution failed: {str(result.get('error') or 'unknown')[:200]}"],
            [], [], {"execution": False},
        )
    hard: list[str] = []
    soft: list[str] = []
    notes: list[str] = []
    checks: dict[str, bool] = {"execution": True}

    columns = result.get("columns")
    rows = result.get("rows")
    if rows is None:
        rows = result.get("results")
    if rows is not None and not isinstance(rows, list):
        hard.append("rows is not a list")
        return _finish(hard, soft, notes, {**checks, "structure": False})
    checks["structure"] = True

    if isinstance(columns, list) and isinstance(rows, list):
        n = len(columns)
        ragged = [i for i, r in enumerate(rows) if isinstance(r, (list, tuple)) and len(r) != n]
        if ragged:
            hard.append(f"ragged rows (row/col mismatch) at indexes {ragged[:3]}")
        checks["row_shape"] = not ragged

    if required_columns and isinstance(columns, list):
        col_names = {str(c).lower() for c in columns}
        missing = [c for c in required_columns if str(c).lower() not in col_names]
        if missing:
            hard.append(f"required columns missing: {', '.join(missing)}")
        checks["required_columns"] = not missing

    if isinstance(rows, list) and len(rows) == 0:
        # A valid query with zero rows is a SUCCESS — noted, never an error.
        notes.append("result is empty (0 rows) — verify filters and date range")

    provenance = result.get("source_id") or result.get("citations") or result.get("source")
    if provenance:
        checks["provenance"] = True
    else:
        soft.append("no provenance (source_id/citations) — findings cannot be attributed")

    if max_age_days is not None:
        as_of_raw = result.get("as_of") or result.get("generated_at")
        if as_of_raw:
            try:
                as_of = datetime.fromisoformat(str(as_of_raw).replace("Z", "+00:00"))
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - as_of).total_seconds() / 86400
                if age > max_age_days:
                    soft.append(
                        f"data is stale: as_of {as_of.date()} exceeds the "
                        f"{max_age_days:g}-day freshness window"
                    )
            except (ValueError, TypeError):
                soft.append("freshness could not be determined (unparseable as_of)")
        else:
            soft.append("freshness unknown (no as_of timestamp)")

    return _finish(hard, soft, notes, checks)


def compare_metric_values(a: float | None, b: float | None, tolerance: float = 0.01) -> str:
    """Cross-source numerical consistency for ONE metric.

    Returns ``consistent`` | ``contradiction`` | ``incomplete``. Callers must
    EXPLAIN contradictions — never silently choose the convenient figure.
    """
    if a is None or b is None:
        return "incomplete"
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return "incomplete"
    denom = max(abs(fa), abs(fb), 1e-9)
    if abs(fa - fb) / denom <= tolerance:
        return "consistent"
    return "contradiction"


def validate_retrieval(
    items: list[dict],
    *,
    relevance_threshold: float = 0.6,
) -> EvidenceValidation:
    """Validate a RAG retrieval set: citations present, relevance coverage.
    Distinguishes retrieved evidence from model inference by requiring a
    source per item. Insufficient coverage is PARTIAL (a limitation to
    disclose), not invalid."""
    checks: dict[str, bool] = {"retrieval_ran": bool(items)}
    hard: list[str] = []
    soft: list[str] = []
    notes: list[str] = []
    if not items:
        soft.append("retrieval returned no documents — insufficient source coverage")
        return _finish(hard, soft, notes, checks)

    no_source = [i for i, it in enumerate(items) if not (it or {}).get("source")]
    if no_source:
        soft.append(f"items without citations (indexes {no_source[:3]}) — cannot attribute evidence")
    else:
        checks["citations"] = True

    scored = [(it or {}).get("score") for it in items]
    scored = [s for s in scored if isinstance(s, (int, float))]
    if scored and max(scored) < relevance_threshold:
        soft.append(
            f"insufficient source coverage: best relevance {max(scored):.2f} "
            f"below threshold {relevance_threshold:.2f}"
        )
    return _finish(hard, soft, notes, checks)


def validate_delegation_result(result: dict) -> EvidenceValidation:
    """Validate a specialist delegation result: task contract present, evidence
    references when expected, and EXECUTION success distinguished from
    successful business OUTCOME (partial/truncated)."""
    if not isinstance(result, dict) or not result.get("success"):
        return _finish(
            [f"delegation failed: {str((result or {}).get('error') or 'unknown')[:200]}"],
            [], [], {"execution": False},
        )
    hard: list[str] = []
    soft: list[str] = []
    notes: list[str] = []
    checks: dict[str, bool] = {"execution": True}

    if result.get("agent_task_contract"):
        checks["task_contract"] = True
    else:
        soft.append("no task contract on the delegation result — provenance of the objective is unverified")

    if result.get("evidence_ids"):
        checks["evidence"] = True
    else:
        soft.append("no evidence references — findings are unattributed")

    if result.get("truncated") or result.get("status") == "partial":
        soft.append("execution succeeded but the business outcome is PARTIAL (truncated result)")
    return _finish(hard, soft, notes, checks)
