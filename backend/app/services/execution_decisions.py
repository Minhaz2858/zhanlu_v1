"""Observation-driven execution decision model (Phase 2.2, 2026-10-31).

ROOT CAUSE ADDRESSED
--------------------
Reviewer verdicts and tool outcomes reached the parent LLM as prose; nothing
mapped an OBSERVATION (tool result classification × evidence state × budget)
to a STRUCTURED next action. This module is that pure decision table — no
LLM call, no chain-of-thought, fully testable.

Taxonomy (mission §4.1): continue | retry | correct_arguments |
select_alternative_tool | delegate_specialist | request_peer_collaboration |
replan | request_clarification | request_approval | complete | fail.

Every decision records: triggering observation, evidence references, reason
code, affected task, proposed next action, budget impact and whether human
authorization is required. The decision NEVER routes around an authorization
boundary and NEVER fabricates success.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

DECISION_TYPES: frozenset[str] = frozenset({
    "continue", "retry", "correct_arguments", "select_alternative_tool",
    "delegate_specialist", "request_peer_collaboration", "replan",
    "request_clarification", "request_approval", "complete", "fail",
})


@dataclass(frozen=True)
class ExecutionObservation:
    """What actually happened — fed by the failure classifier and the
    evidence registry. Pure data, no model output."""
    is_success: bool = False
    category: str = ""                     # FailureCategory value or ""
    terminal: bool = False
    approval_pending: bool = False
    is_empty: bool = False
    is_partial: bool = False
    requires_modified_arguments: bool = False
    attempts: int = 0
    budget_left: bool = True
    has_alternative: bool = False          # authorized alternative capability found
    specialist_available: bool = False     # published compatible specialist exists
    missing_evidence: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    failed_criteria: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    triggering_observation: str = ""
    objective: str = ""


@dataclass(frozen=True)
class ExecutionDecision:
    decision: str
    triggering_observation: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    reason_code: str = ""
    affected_task: str | None = None
    proposed_next_action: str = ""
    budget_impact: str = "none"
    requires_authorization: bool = False
    objective: str = ""

    def to_dict(self) -> dict:
        """Structured, persistable, event-ready. NEVER carries private
        chain-of-thought — only the fields defined above."""
        return {
            "decision": self.decision,
            "triggering_observation": self.triggering_observation[:300],
            "evidence": list(self.evidence)[:10],
            "reason_code": self.reason_code,
            "affected_task": self.affected_task,
            "proposed_next_action": self.proposed_next_action[:300],
            "budget_impact": self.budget_impact,
            "requires_authorization": self.requires_authorization,
            "objective": self.objective[:300],
        }


def _d(
    decision: str,
    obs: ExecutionObservation,
    reason: str,
    action: str,
    *,
    evidence: Iterable[str] = (),
    affected: str | None = None,
    budget_impact: str = "none",
    requires_authorization: bool = False,
) -> ExecutionDecision:
    assert decision in DECISION_TYPES
    return ExecutionDecision(
        decision=decision,
        triggering_observation=obs.triggering_observation,
        evidence=tuple(evidence),
        reason_code=reason,
        affected_task=affected,
        proposed_next_action=action,
        budget_impact=budget_impact,
        requires_authorization=requires_authorization,
        objective=obs.objective,
    )


_MAX_ARG_ATTEMPTS = 2
_MAX_TRANSIENT_ATTEMPTS = 2
_MAX_UNKNOWN_ATTEMPTS = 1


def evaluate_observation(obs: ExecutionObservation) -> ExecutionDecision:
    """The decision table. Order encodes safety precedence:

    authorization > determinism > transient recovery > evidence gaps >
    completion. Every branch is bounded and honest.
    """
    # 1. Authorization first — never routed around, never retried.
    if obs.approval_pending:
        return _d(
            "request_approval", obs, "approval_pending",
            "Pause for human approval; the operation must not proceed or be "
            "rerouted automatically.",
            requires_authorization=True,
        )
    if obs.terminal and obs.category == "permission_denied":
        return _d(
            "fail", obs, "authorization_boundary",
            "Report the authorization limitation honestly; never bypass or "
            "try an unauthorized route.",
            requires_authorization=True,
        )

    # 2. Deterministic request problems.
    if not obs.is_success and obs.category in (
        "invalid_arguments", "schema_validation_error", "missing_required_parameter",
    ):
        if obs.attempts < _MAX_ARG_ATTEMPTS:
            return _d(
                "correct_arguments", obs, "invalid_arguments",
                "Reconstruct arguments against the tool schema using the "
                "failure feedback; retry once.",
                budget_impact="one_reformulation",
            )
        return _d(
            "request_clarification", obs, "argument_correction_exhausted",
            "Ask the user for the missing parameter or constraint.",
        )
    if not obs.is_success and obs.category == "unsupported_operation":
        if obs.has_alternative:
            return _d(
                "select_alternative_tool", obs, "operation_unsupported_alt_available",
                "Switch to the verified authorized alternative capability.",
                budget_impact="one_alternative_attempt",
            )
        return _d(
            "fail", obs, "operation_unsupported",
            "Report the limitation; no authorized capability supports this "
            "operation.",
        )

    # 3. Transient infrastructure failures — bounded recovery.
    if not obs.is_success and obs.category in ("timeout", "connection_error", "rate_limit"):
        if obs.attempts < _MAX_TRANSIENT_ATTEMPTS and obs.budget_left:
            return _d(
                "retry", obs, "transient_failure",
                "One bounded retry with backoff (read-only/idempotent only).",
                budget_impact="one_retry",
            )
        return _d(
            "replan", obs, "transient_exhausted",
            "Regenerate the plan avoiding the failed path; preserve completed "
            "results and the original objective.",
            budget_impact="one_replan",
        )
    if not obs.is_success and obs.category == "unknown_error":
        if obs.attempts < _MAX_UNKNOWN_ATTEMPTS and obs.budget_left:
            return _d(
                "retry", obs, "unknown_error_first_retry",
                "Retry once within budget (read-only operations only); "
                "classify the next failure before any further action.",
                budget_impact="one_retry",
            )
        return _d(
            "fail", obs, "unknown_error_unresolved",
            "Report the limitation honestly; do not guess an outcome.",
        )
    if not obs.is_success:
        # Any other classified failure (database_error, resource_not_found,
        # malformed_output, provider_error, resource_unavailable, …).
        if obs.category == "resource_not_found":
            return _d(
                "correct_arguments", obs, "resource_not_found",
                "Discover valid resources (list/describe) and correct the "
                "reference; never invent one.",
            )
        if obs.category == "malformed_output" and obs.budget_left:
            return _d(
                "retry", obs, "malformed_output",
                "Re-execute safely once; validate the returned structure.",
                budget_impact="one_retry",
            )
        if obs.budget_left:
            return _d(
                "replan", obs, "recoverable_failure",
                "Regenerate the plan around the failure; preserve verified "
                "evidence.",
                budget_impact="one_replan",
            )
        return _d(
            "fail", obs, "recovery_budget_exhausted",
            "Report the failure and what was verified so far.",
        )

    # ── Success paths ──────────────────────────────────────────────────
    if obs.is_empty:
        return _d(
            "continue", obs, "empty_result_not_failure",
            "Verify filters, date range and data availability (probe the "
            "latest posted date) before concluding; a zero-row result is a "
            "success with no rows, not an error.",
        )
    if obs.contradictions:
        if obs.specialist_available:
            return _d(
                "request_peer_collaboration", obs, "contradictory_evidence",
                "Request peer verification; inspect freshness, authority and "
                "provenance of both sources; never silently pick a number.",
                affected=obs.contradictions[0][:120],
            )
        return _d(
            "replan", obs, "contradictory_evidence",
            "Resolve the discrepancy by re-querying authoritative sources; "
            "explain unresolved differences in the final answer.",
        )
    if obs.unsupported_claims:
        return _d(
            "replan", obs, "unsupported_claims",
            "Remove or substantiate the unsupported claims with real "
            "evidence; never present them as verified.",
            affected=obs.unsupported_claims[0][:120],
        )
    if obs.failed_criteria:
        if obs.specialist_available:
            return _d(
                "delegate_specialist", obs, "failed_acceptance_criteria",
                "Delegate the failed criterion to a published specialist.",
                affected=obs.failed_criteria[0][:120],
                budget_impact="one_delegation",
            )
        return _d(
            "replan", obs, "failed_acceptance_criteria",
            "Revise the approach to satisfy the failed criterion or report "
            "the gap honestly.",
            affected=obs.failed_criteria[0][:120],
            budget_impact="one_replan",
        )
    if obs.missing_evidence:
        if obs.specialist_available:
            return _d(
                "delegate_specialist", obs, "missing_evidence",
                "Delegate targeted evidence gathering to the published "
                "specialist; recalculate only when the evidence arrives.",
                affected=obs.missing_evidence[0][:120],
                budget_impact="one_delegation",
            )
        return _d(
            "replan", obs, "missing_evidence",
            "Search authorized sources for the missing evidence; if it does "
            "not exist, finish with explicit limitations — never invent it.",
            affected=obs.missing_evidence[0][:120],
            budget_impact="one_replan",
        )
    if obs.is_partial:
        return _d(
            "continue", obs, "partial_evidence",
            "Report the verified portion and identify exactly what remains "
            "missing.",
        )
    return _d(
        "complete", obs, "objective_satisfied",
        "Produce the final answer from the verified evidence.",
    )


def decision_from_review(
    review: dict | None,
    *,
    specialist_available: bool = False,
    objective: str = "",
) -> ExecutionDecision:
    """P2.3 seam: a structured reviewer report → an ExecutionDecision via the
    same table. `no_review` continues (no fake decisions). The PRIMARY gap
    follows the Phase-1 replan decision table (``recommended_next_action``):
    gather_missing_evidence > revise_response > resolve_contradictions >
    address_failed_criteria — the review's own precedence, not ours."""
    from app.services.agent_collaboration.reviewer import ReviewReport
    from app.services.synexia.replan_policy import evaluate_reviewer_findings

    if not isinstance(review, dict) or not review:
        return _d(
            "continue", ExecutionObservation(objective=objective),
            "no_review", "Proceed with the current evidence.",
        )
    # Normalize THROUGH the reviewer schema first: dimension warn/fail
    # details deterministically derive the structured gap fields (Phase 2.3)
    # — a dimensions-only payload still yields structured findings.
    try:
        normalized = ReviewReport.from_dict(review).to_dict()
    except Exception:  # noqa: BLE001 — malformed review falls through raw
        normalized = review
    findings = evaluate_reviewer_findings(normalized)
    missing = tuple(findings.get("missing_evidence") or ())
    unsupported = tuple(findings.get("unsupported_claims") or ())
    contradictions = tuple(findings.get("contradictions") or ())
    failed = tuple(findings.get("failed_acceptance_criteria") or ())
    action = findings.get("recommended_next_action")

    obs = ExecutionObservation(
        is_success=True,
        # ROOT FIX (enterprise reliability milestone, 2026-11-01): the
        # reviewer's derived gaps are passed through IN FULL. Previously only
        # the field matching ``recommended_next_action`` was copied, so a
        # needs_revision review whose primary action was e.g.
        # ``revise_response`` reached the decision table as a CLEAN SUCCESS
        # and decided "complete / objective_satisfied" — the reviewer's
        # verdict was silently converted into completion. The decision
        # table's own precedence (authorization > determinism > ... > gaps >
        # completion) picks the branch; nothing the reviewer found is
        # dropped or flattened.
        missing_evidence=missing,
        unsupported_claims=unsupported,
        contradictions=contradictions,
        failed_criteria=failed,
        specialist_available=specialist_available,
        objective=objective,
        triggering_observation=(
            f"reviewer verdict={findings.get('verdict') or 'none'} "
            f"action={action or 'none'}; "
            + "; ".join(filter(None, [
                f"missing: {', '.join(missing)}" if missing else None,
                f"unsupported: {', '.join(unsupported)}" if unsupported else None,
                f"contradictions: {', '.join(contradictions)}" if contradictions else None,
                f"failed criteria: {', '.join(failed)}" if failed else None,
            ])) or "reviewer found no gaps"
        ),
    )
    if findings.get("should_replan") is False:
        return _d(
            "complete", obs, findings.get("reason") or "reviewer_approved",
            "Reviewer approved — produce the final answer from the verified "
            "evidence.",
        )
    # I-7 ROOT FIX (enterprise reliability milestone, 2026-11-01): a
    # should_replan verdict can NEVER decide "complete". The observation
    # table still picks the nuanced branch (delegate_specialist,
    # request_peer_collaboration, replan, …) from the reviewer's derived
    # gaps — but its clean-success completion branch is overridden, because
    # with a verdict-level revision (no structured gap fields) the table
    # used to decide "complete / objective_satisfied", silently converting
    # the reviewer's needs_revision into completion.
    decision = evaluate_observation(obs)
    if decision.decision == "complete":
        _all_gaps = (*missing, *unsupported, *contradictions, *failed)
        return _d(
            "replan",
            obs,
            findings.get("reason") or "reviewer_needs_revision",
            "The reviewer flagged gaps in the result — revise the response "
            "or gather the missing evidence; never present unverified "
            "content as complete.",
            affected=_all_gaps[0][:120] if _all_gaps else None,
            budget_impact="one_replan",
        )
    # Preserve the SECONDARY gaps as evidence references on the decision so
    # nothing the reviewer found is silently dropped.
    secondary = tuple(
        g for g in (*missing, *unsupported, *contradictions, *failed)
        if g != (decision.affected_task or "")
    )
    if secondary:
        decision = ExecutionDecision(
            decision=decision.decision,
            triggering_observation=decision.triggering_observation,
            evidence=secondary[:10],
            reason_code=decision.reason_code,
            affected_task=decision.affected_task,
            proposed_next_action=decision.proposed_next_action,
            budget_impact=decision.budget_impact,
            requires_authorization=decision.requires_authorization,
            objective=decision.objective,
        )
    return decision
