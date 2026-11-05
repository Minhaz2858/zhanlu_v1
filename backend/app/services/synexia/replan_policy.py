"""Which failed plan nodes are worth a corrective re-plan — one policy, two callers.

Two recovery loops exist and both used to answer this question with their own
hardcoded node-type filter, so they could drift apart:

* ``synexia/fsm.py::_should_verify_replan`` — VERIFY drives a re-plan (loop back
  to PLAN) when a CRITICAL check failed.
* ``synexia/capability_router.py::_recoverable_failures`` — ACT generates a
  corrective sub-plan after the main pass.

Both consulted ``node_type in ("tool", "skill")`` and both documented the same
exclusion: the data pipeline is never re-planned. The consequence was user
visible — an ``nl2sql`` node that died on a bad identifier ("Unknown column
'state'") could not be retried by the runtime and the turn went straight to
FINALIZE with an essay asking the user to name the column, even though the
database had already said what was wrong.

Naming failures are now repaired inside the query executor itself
(``app/services/db/nl_answer_service.py``, bounded by
``NL2SQL_DB_ERROR_REPAIR_MAX``), so what remains here is the *structural* class
of failure — wrong table picked, catalog empty, view missing — where a
different plan genuinely can fix it. That is why ``nl2sql`` is eligible below
but ``synthesize``/``sandbox`` are not: they replay an already-failed input and
a new plan adds no information.

Eligibility, in one place:

* node_type in ``REPLANNABLE_NODE_TYPES``
* status == "failed"
* the error is not one of ``NON_ACTIONABLE_MARKERS`` (policy denials would just
  re-deny; credential/connection/timeout/lock errors reproduce identically on
  the next attempt and only burn budget).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# General tool/skill steps plus the query pipeline's structural-failure class.
REPLANNABLE_NODE_TYPES: tuple[str, ...] = ("tool", "skill", "nl2sql")

# Replaying these with a fresh plan cannot help: they consume the previous
# node's output, which is exactly what failed.
NON_REPLANNABLE_DATA_NODE_TYPES: tuple[str, ...] = ("synthesize", "sandbox")

# Lowercase substrings that mark a failure as environmental / non-actionable.
# These reproduce identically on a retry, so re-planning them is pure budget
# burn (and for policy denials, re-denial).
NON_ACTIONABLE_MARKERS: tuple[str, ...] = (
    "policy denied",
    "permission denied",
    "access denied",
    "not authorized",
    "unauthorized",
    "authentication",
    "forbidden",
    "connection refused",
    "connection reset",
    "cannot connect",
    "can't connect",
    "could not connect",
    "no such host",
    "timed out",
    "timeout",
    "deadlock",
    "lock wait",
    "temporarily unavailable",
    # 2026-10-29 cycle 2: SQL-validator policy rejections. A read-only /
    # multi-statement / blocked-construct verdict is a deterministic denial
    # — regenerating SQL or re-planning the node just burns pipeline budget
    # (the 2026-10-29 replay burned ~6 minutes on six rejected UNION-shaped
    # candidates). Note: legitimate UNION/EXCEPT/INTERSECT of SELECTs are
    # no longer rejected at all (nl2sql/validator.py Rule 1) — this marker
    # only catches genuinely disallowed shapes.
    "only select statements are allowed",
    "multi-statement sql is not allowed",
    "blocked statement type",
    "turn_sql_budget_exhausted",
    "allowed sql attempts",
)


def is_non_actionable_error(error: Optional[str]) -> bool:
    """True when re-planning cannot help because the failure is environmental."""
    low = (error or "").strip().lower()
    if not low:
        return False
    return any(marker in low for marker in NON_ACTIONABLE_MARKERS)


def is_replannable_failure(node_type: Optional[str], error: Optional[str]) -> bool:
    """True when a failed node of this type/error is worth a corrective plan."""
    if node_type not in REPLANNABLE_NODE_TYPES:
        return False
    return not is_non_actionable_error(error)


def evaluate_reviewer_findings(review: Optional[dict]) -> dict:
    """P1.4 (Phase 1): structured reviewer verdict → replan decision.

    The reviewer's fact-safe report (``agent_collaboration/reviewer.py``) is
    turned into an explicit decision the parent LLM can act on:
    ``should_replan`` + ``recommended_next_action`` + the concrete gaps
    (missing evidence / unsupported claims / contradictions / failed
    acceptance criteria). MISSING EVIDENCE IS PRESERVED VERBATIM — the
    decision never invents facts, it only names what is absent.

    Pure function; the caller decides what to do (the existing bounded
    targeted re-investigation in ``delegate_tool`` remains the active
    mechanism — this adds the structured, observable decision layer).
    """
    base = {
        "should_replan": False,
        "needs_evidence": False,
        "missing_evidence": [],
        "unsupported_claims": [],
        "contradictions": [],
        "failed_acceptance_criteria": [],
        "recommended_next_action": None,
        "reason": "no_review",
        "verdict": None,
    }
    if not isinstance(review, dict) or not review:
        return base

    verdict = str(review.get("verdict") or review.get("overall") or "").strip().lower()
    missing = [str(m) for m in (review.get("missing_evidence") or []) if str(m).strip()]
    unsupported = [str(c) for c in (review.get("unsupported_claims") or []) if str(c).strip()]
    contradictions = [str(c) for c in (review.get("contradictions") or []) if str(c).strip()]
    failed_criteria = [str(c) for c in (review.get("failed_acceptance_criteria") or []) if str(c).strip()]
    needs_evidence = bool(review.get("needs_evidence")) or bool(missing)

    if "is_complete" in review:
        is_complete = bool(review.get("is_complete"))
    else:
        is_complete = verdict in ("approved", "pass", "complete", "completed")

    out = {
        **base,
        "verdict": verdict or None,
        "needs_evidence": needs_evidence,
        "missing_evidence": missing,
        "unsupported_claims": unsupported,
        "contradictions": contradictions,
        "failed_acceptance_criteria": failed_criteria,
    }

    has_gaps = bool(missing or unsupported or contradictions or failed_criteria)
    if is_complete and verdict in ("approved", "pass", "complete", "completed") and not has_gaps:
        out["reason"] = "reviewer_approved"
        return out

    out["should_replan"] = True
    if missing or needs_evidence:
        out["recommended_next_action"] = "gather_missing_evidence"
        out["reason"] = "missing_evidence"
    elif unsupported:
        out["recommended_next_action"] = "revise_response"
        out["reason"] = "unsupported_claims"
    elif contradictions:
        out["recommended_next_action"] = "resolve_contradictions"
        out["reason"] = "contradictions"
    elif failed_criteria:
        out["recommended_next_action"] = "address_failed_criteria"
        out["reason"] = "failed_acceptance_criteria"
    else:
        out["recommended_next_action"] = "revise_response"
        out["reason"] = "reviewer_needs_revision"
    return out


def collect_replannable_failures(nodes: Iterable[Any]) -> list[dict]:
    """Failed nodes worth a re-plan, as the failure-context dicts the planner reads.

    Accepts any object carrying ``name`` / ``node_type`` / ``status`` / ``error``
    (the ORM ``PlanNode`` and the FSM's detached node snapshots both qualify).
    """
    out: list[dict] = []
    for node in nodes or ():
        if getattr(node, "status", None) != "failed":
            continue
        node_type = getattr(node, "node_type", None)
        err = getattr(node, "error", None) or ""
        if not is_replannable_failure(node_type, err):
            continue
        out.append({
            "name": getattr(node, "name", None),
            "node_type": node_type,
            "error": err or "unknown error",
            # 2B-4: durable-observation identity — the corrective planner
            # enrichment reads the node's structured scope verdict by it.
            "node_id": getattr(node, "id", None),
        })
    return out
