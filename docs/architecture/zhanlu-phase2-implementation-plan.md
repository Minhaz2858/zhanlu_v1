# Zhanlu Phase 2 — Implementation Plan

> Execution: TDD (RED before GREEN per task), systematic-debugging for failures.
> No commits unless explicitly authorized. Foreign uncommitted WIP is not touched.

**Goal:** intelligent, bounded, observable recovery for the existing execution runtime.

**Constraints:** reuse ReAct runtime, turn planner, replan policy, registry, broker,
contracts, reviewer, evidence registry, governance, persistence, scheduler. No new
orchestrator/retry framework/registries.

## Task order (each = RED test → minimal implementation → focused run → regression)

### T1 — Failure taxonomy module (P2.1 core)
- Test: `backend/tests/test_execution_recovery.py`
  - 16-category enum complete; `classify_failure` precedence (typed fields > exception >
    markers); empty-result ≠ failure; permission terminal; timeout/connection/rate-limit
    classification incl. Retry-After hint; malformed JSON args → schema_validation_error.
- Impl: `app/services/execution_recovery.py` (pure).

### T2 — Bounded healing policy
- Test: same file — `should_retry`: never permission; writes need idempotent;
  invalid-args needs modification; per-call attempts; per-task budget exhaustion.
- Impl: `RecoveryPolicy` + settings flags (`EXEC_RECOVERY_*`) in `app/config.py`.

### T3 — Wire into execute_tool_with_retry (smallest seam)
- Test: `backend/tests/test_tool_retry_classification.py` — failure dicts now carry
  `failure_category` + `recovery_hint`; permission never retried; mutating tools not
  retried without idempotent marker; reformulation only when
  `requires_modified_arguments`.
- Modify: `app/services/agent_tools.py` (marker fallback replaced by classifier; typed
  `failure_kind` path unchanged).

### T4 — Execution decision model (P2.2)
- Test: `backend/tests/test_execution_decisions.py` — 11-value taxonomy; decision table
  (classification × state → continue/retry/correct_arguments/select_alternative_tool/
  delegate_specialist/replan/request_clarification/request_approval/complete/fail);
  continuity fields (objective, completed results, evidence preserved); no CoT.
- Impl: `app/services/execution_decisions.py`; attach to reviewed delegations +
  persist in invocation output.

### T5 — Reviewer gap producers (P2.3)
- Test: `backend/tests/test_reviewer_findings_structured.py` — reviewer emits
  `unsupported_claims` (claims w/o evidence refs), `contradictions` (conflicting
  figures), `failed_acceptance_criteria`; loop stays bounded (existing tests pass).
- Modify: `app/services/agent_collaboration/reviewer.py` only.

### T6 — Capability fallback (P2.4)
- Test: `backend/tests/test_capability_fallback.py` — published-only, capability match,
  authority rationale, refusal when none/unauthorized, never for destructive ops.
- Impl: `app/services/capability_fallback.py`; advisory field on delegation failure.

### T7 — Evidence validation (P2.5)
- Test: `backend/tests/test_evidence_validation.py` — rows/columns shape, provenance,
  freshness, contradiction detection, partial/invalid verdicts; NL2SQL gates untouched.
- Impl: `app/services/evidence_validation.py`; hooked at data-result observation point.

### T8 — Approval hardening (P2.7)
- Test: `backend/tests/test_approval_hardening.py` — destructive tier requires
  confirmation even for meta-agents (flag `EXEC_APPROVAL_DESTRUCTIVE_ALWAYS=True`);
  read/draft unaffected; approval results never retried.
- Modify: `app/services/agent_tools.py` meta-agent bypass condition only.

### T9 — Observability (P2.8)
- Test: backend `test_recovery_observability.py` (typed events, scrubbed, capped, real
  ops only) + frontend `LiveActivityStream.test.jsx` (decision/classification rows).
- Modify: `agents.py` emission points (additive), `sse_builders.py` templates,
  `LiveActivityStream.jsx`.

### T10 — Scenarios A–H
- Test: `backend/tests/test_phase2_scenarios.py` — deterministic, mocks only; each
  scenario exercises the real seam (classifier → decision → policy → SSE shape).

### T11 — Regression + build + docs
- Full adjacent-suite pytest sweep, `npm run build`, update progress log + roadmap,
  final report (implemented / integration-tested / live-verified / blocked).

## Rollback
Every change is additive and flag-gated; revert the module + its wire-in lines per task.
Settings defaults preserve current behavior except: unknown-error default (now classified
`unknown_error`, bounded) and destructive-tier meta-agent gating (flag, default true).
