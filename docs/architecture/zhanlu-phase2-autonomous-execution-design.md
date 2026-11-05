# Zhanlu Phase 2 — Autonomous Execution Intelligence: Design

Status: DESIGN + AUDIT + IMPLEMENTATION RECORD (2026-10-31). Evidence
classification follows the three-state rule: implemented ≠ tested ≠
production-ready. Everything below is INTEGRATION-TESTED (deterministic
mocks); nothing here is live-verified yet.

## 0. Implementation record (2026-10-31)

| Piece | Module | Tests |
|---|---|---|
| P2.1 failure taxonomy + bounded healing | `app/services/execution_recovery.py` (NEW) | `test_execution_recovery.py` 31 |
| P2.1 wire-in (retry seam) | `agent_tools.execute_tool_with_retry` (rewired) | `test_tool_retry_classification.py` 9 |
| P2.2 execution decision model | `app/services/execution_decisions.py` (NEW) | `test_execution_decisions.py` 20 |
| P2.3 reviewer structured gaps | `agent_collaboration/reviewer.py` (extended) | `test_reviewer_findings_structured.py` 10 |
| P2.3/2.4 delegation wiring | `delegate_tool.py` (execution_decision + alternatives) | `test_delegate_phase2_wiring.py` 5 |
| P2.4 capability fallback | `app/services/capability_fallback.py` (NEW) | `test_capability_fallback.py` 8 |
| P2.5 evidence validation | `app/services/evidence_validation.py` (NEW) | `test_evidence_validation.py` 18 |
| P2.7 approval hardening | `agent_tools.py` meta-bypass (destructive always gated) | `test_approval_hardening.py` 4 |
| P2.8 observability | `agents.py` + `sse_builders.py` + `LiveActivityStream.jsx` | backend template test + frontend `recoveryEventLabel` 4 |
| Scenarios A–H | cross-module | `test_phase2_scenarios.py` 9 |

Sweep: 34 adjacent suites — 475 passed; 4 failures ALL verified pre-existing
via git-stash A/B (replan_policy `node_id` ×2 = foreign "2B-4" WIP;
`parallel_tools_and_approval` order-fragility = pre-existing cross-test
schema pollution from `test_access_policy_enforcement`; loop-guard source
regex ×1 = foreign WIP). Frontend production build: **exit 0**.

Key wiring facts:
- `execute_tool_with_retry` now classifies EVERY failure (typed fields >
  exception > markers), never retries permission/terminal failures, never
  retries mutating tools without idempotency evidence, only reformulates
  arg-fixable categories, honors the per-turn `HealingBudget`
  (`context["_healing_budget"]`, created once per v3 turn), and attaches
  `failure_category`/`recovery_hint`/`recovery_outcome` to every returned
  failure dict — structured observation for the ReAct loop.
- `decision_from_review` normalizes reviewer payloads through
  `ReviewReport.from_dict` (dimension details → structured gaps) then routes
  via the Phase-1 `evaluate_reviewer_findings` decision-table precedence.
- SSE: `failure_classified` + `execution_decision` typed events (declared in
  the template-coverage test), emitted ONLY for operations that actually ran.


## 1. Root-cause audit (investigation before modification)

### 1.1 What already exists (verified — REUSE, do not rebuild)

| Concern | Existing mechanism | Location |
|---|---|---|
| Schema validation of tool args | `validate_tool_args` (jsonschema, registry + CRUD schemas) | `app/services/tool_arg_validator.py:56` |
| Handler retry + backoff + one-shot LLM arg reformulation | `run_tool_with_reliability`, `reformulate_tool_args` | `app/services/agent_tools.py:155-290`, `app/services/tool_retry.py:77-205` |
| Result-level bounded retry | `execute_tool_with_retry` (TOOL_RETRY_MAX_ATTEMPTS=2, reformulation ×1) | `app/services/agent_tools.py:386-472` |
| Loop guards (exact-failure / same-tool / no-progress) | `ToolLoopGuardController` warn/block/halt | `app/services/tool_loop_guardrails.py:124-287` |
| Iteration + wall-clock budgets | `IterationBudget`, `TURN_WALL_*` dynamic tiers | `app/services/iteration_budget.py`, `config.py:311-329` |
| Typed failure kinds (data domain) | `FailureKind`, `NON_RETRYABLE_FAILURE_KINDS` | `app/services/db/result_contract.py:52-80` |
| Replannable/non-actionable taxonomy | `NON_ACTIONABLE_MARKERS`, `is_replannable_failure` | `app/services/synexia/replan_policy.py:49-95` |
| Corrective replan loops | FSM VERIFY loop + ACT corrective sub-plan (both bounded, budget-gated) | `synexia/fsm.py:386-426`, `synexia/capability_router.py:300-320` |
| Reviewer → structured decision | `evaluate_reviewer_findings` (Phase 1) | `replan_policy.py:98` |
| Evidence registry with provenance/freshness/validation | `EvidenceItem` | `agent_collaboration/shared_evidence.py:52-112` |
| Agent Task Contract (budgets, deadlines) | `AgentTaskContract`, broker negotiation | `agent_collaboration/contract.py`, `broker.py:116-237` |
| Invocation lifecycle + orphan recovery | two-phase records, terminal-wins, scheduler recovery | `services/agent_invocations.py` |
| Approval gates | `resolve_risk`, `requires_confirmation`, ApprovalService, chat pause/resume | `agent_tools.py:582-680`, `routers/agents.py` |
| Empty ≠ failure | `ResultStatus.EMPTY` + smart retry on metadata-only rows | `result_contract.py:36-78`, `agents.py:17679` |
| Recovery SSE event | `retry` event ("Correcting {target}") rendered in LiveActivityStream | `sse_builders.py:139`, LiveActivityStream.jsx:777 |

### 1.2 Root causes / gaps (ranked)

- **G1 — Fragmented failure classification (P0).** Three unrelated taxonomies:
  string markers in `agent_tools` (`_PERMANENT/_TRANSIENT_ERROR_MARKERS`), typed
  `FailureKind` (data tools only), `NON_ACTIONABLE_MARKERS` (synexia only). No shared
  category enum; `invalid_arguments`, `rate_limit`, `timeout`, `permission_denied` do not
  exist as typed categories for general tools. Recovery strategy therefore cannot be
  selected systematically — only retry/not-retry.
- **G2 — Unknown errors default to retryable** (`agent_tools.py:338`). The exact default
  that caused the documented RC-1 loop in `result_contract.py:14-26`; guard rails catch
  loops but only after wasted attempts.
- **G3 — Mutating writes are retry-classified without idempotency consultation.**
  `MUTATING_TOOL_NAMES` exists but retry logic never reads it; only `requires_approval`
  is protected. A failed write retry could double-apply.
- **G4 — No structured recovery feedback to the model.** Errors ride into the observation
  as raw strings; the model must re-derive category + recovery from prose every time.
- **G5 — Recovery is invisible.** UI sees terminal done/failed; the existing `retry`
  event is used for one narrow path. No event for "classified failure", "correcting
  arguments", "switching capability", "recovered".
- **G6 — Reviewer gap fields have no producer.** `evaluate_reviewer_findings` reads
  `unsupported_claims`/`contradictions`/`failed_acceptance_criteria`, but
  `review_findings` only emits `needs_evidence` + `issues` — so reviewer-driven
  corrective execution is under-informed.
- **G7 — No capability fallback.** Corrective replans regenerate with the same executor
  set; no authority-aware alternative discovery.
- **G8 — Meta-agent approval bypass is unconditional** (`agent_tools.py:616-621`):
  system meta-agents skip `requires_confirmation` for ALL tiers including destructive.
- **G9 — Single-call fast path propagates raw exceptions** (`tool_executor.py:133-137`),
  escaping the classification seam on the most common path.
- **G10 — No central tool timeout** (audit note `config.py:453`): a hung tool is visible
  only at the turn wall clock.

Non-goals (deliberate): no second orchestrator/retry framework/registry; no
chain-of-thought exposure; no unbounded loops; no changes to SQL validation gates.

## 2. Design

### 2.1 P2.1 — Shared failure taxonomy + bounded healing policy (NEW module,
single authority)

New `app/services/execution_recovery.py`:

- `FailureCategory` enum — the 16 mission categories (`invalid_arguments`,
  `schema_validation_error`, `missing_required_parameter`, `permission_denied`,
  `resource_not_found`, `resource_unavailable`, `connection_error`, `timeout`,
  `rate_limit`, `provider_error`, `database_error`, `empty_result`,
  `malformed_output`, `partial_result`, `unsupported_operation`, `unknown_error`).
- `FailureClassification` dataclass: `category`, `outcome` (`success_with_empty_result` |
  `success_with_partial_data` | `execution_failure` | `authorization_failure` |
  `invalid_request` | `temporary_infrastructure_failure`), `retryable: bool`,
  `requires_modified_arguments: bool`, `terminal: bool` (permission/unsupported),
  `recovery_hint: str` (structured, model-readable — NOT a hardcoded action), `detail`.
- `classify_failure(result=None, error=None, *, tool_name="", mutating=False,
  attempts=0)` — precedence: explicit result fields (`failure_kind`, `error_kind`,
  `requires_approval`, `empty_set`) → exception type → string markers (reusing the
  existing marker lists via import, not copies). Empty result is NOT a failure
  (`success_with_empty_result`, retryable=False, hint = verify filters/date range).
- `RecoveryPolicy` (settings-backed): `EXEC_RECOVERY_MAX_ATTEMPTS_PER_CALL` (default =
  existing TOOL_RETRY_MAX_ATTEMPTS), `EXEC_RECOVERY_MAX_PER_TASK` (new, default 4),
  `EXEC_RECOVERY_WRITE_IDEMPOTENCY_REQUIRED=True`.
- `should_retry(classification, *, attempts, mutating, idempotent, within_budget)` —
  NEVER for `permission_denied`/`unsupported_operation`/authorization outcomes; writes
  only when idempotent; `invalid_arguments`/`missing_required_parameter` only with
  modified arguments; unknown bounded by per-call attempts.

Wire-in (smallest correct change): `agent_tools.execute_tool_with_retry` consults
`classify_failure`/`should_retry` (replacing only the bare-marker fallback), attaches
`failure_category` + `recovery_hint` to failure dicts (structured observation for the
ReAct loop, G4), and records healing attempts against the per-task budget.

### 2.2 P2.2 — Execution decision model (NEW pure module)

`app/services/execution_decisions.py`: `ExecutionDecision` (decision type from the
11-value taxonomy, triggering observation, evidence refs, reason code, affected task,
proposed next action, budget impact, authorization requirement) + `evaluate_observation(...)`
mapping (classification × task state × evidence state) → decision. Persisted on
delegation/invocation output (structured, no chain-of-thought) and emitted as a typed
live event.

### 2.3 P2.3 — Reviewer-driven corrective execution

Extend `review_findings` to produce `unsupported_claims`, `contradictions`,
`failed_acceptance_criteria` (numerical-consistency + criterion checks) alongside the
existing 7 dimensions; keep the bounded loop (1 re-review, ≤2 targeted follow-ups).
`evaluate_reviewer_findings` then drives real decisions via 2.2.

### 2.4 P2.4 — Capability fallback

`app/services/capability_fallback.py`: `find_alternative_capability(...)` — discovers
alternative PUBLISHED AgentApps (existing `list_available_agents` path) / registry tools
compatible with the failed capability; refuses unpublished/unauthorized/unrelated
candidates; returns candidates with authority rationale or an explicit refusal. Advisory
to the decision model; never auto-executes writes.

### 2.5 P2.5 — Evidence validation

`app/services/evidence_validation.py`: `validate_tool_output(...)` — structure (rows/
columns/shape), required fields, provenance presence, freshness vs EvidenceItem,
numerical consistency (invariant spot-checks), contradictions; returns
`validated|partial|invalid` + issues. NL2SQL keeps ALL existing gates (validator,
execution gate, source verification) — this layer validates the RETURNED DATA, never
rewrites SQL, never invents calculations. RAG: citation presence + relevance flags.

### 2.6 P2.6 — Long-running recovery (verify + document, no new scheduler)

H1 two-phase lifecycle + `recover_orphaned_invocations` (scheduler) already cover worker
crash → no silent running states. Verified by existing adversarial tests; this phase adds
the missing crash-resume DOCUMENTATION decision (failed-node-level re-execution is a
future roadmap item, not built here) and keeps budgets shared (`may_refine`).

### 2.7 P2.7 — Human approval

Mechanism exists end-to-end. Gap G8 closed flag-gated: destructive-tier tools require
confirmation even for SYSTEM_META_AGENTS when `EXEC_APPROVAL_DESTRUCTIVE_ALWAYS=true`
(default true). `requires_approval` results are never retried or reformulated (already
true — pinned by tests).

### 2.8 P2.8 — Observability

Additive typed live events on the existing `_push_live_event` pattern:
`failure_classified`, `execution_decision` (label_key templates, content-invariant
scrubbed, capped). Frontend: LiveActivityStream renders decision/classification compact
rows. No new panels.

## 3. Scenario mapping (A–H)

A invalid args → 2.1 classification + modified-arg retry; B timeout → bounded retry;
C missing data → reviewer gaps + evidence validation + honest limitation; D multi-agent
partial → delegation status + decision `replan`/`complete-partial`; E contradictions →
reviewer contradictions + provenance/freshness inspection; F specialist unavailable →
fallback discovery refusal; G consequential action → approval gate (+ destructive
always-gated); H interruption → orphan recovery (verified).
