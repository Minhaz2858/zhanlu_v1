# EBOM Production Readiness — Implementation Spec

Status: EXECUTION GUIDE (Stage 2 of the Production Readiness Program).
Starting point: commit `faa566314` on `fix/automation-chat-artifacts-ordering`.
Supersedes nothing; extends `enterprise-business-operating-model-redesign.md`.

## 0. Audit findings (evidence for every design decision)

| Gap found | Evidence | Decision |
|---|---|---|
| No outcome measurement model anywhere | `grep -rl "outcome\|realized\|baseline_value" app/models/` → nothing | B2: new additive `outcome_measurements` table (migration 113) |
| No ERP write-back connector | `app/services/datasources/` = read-side only (mysql/postgres/sqlite); PO drafts end at "buyer exports" | C4: connector-neutral contract + sandbox adapter, executed ONLY via `execute_approved_action` |
| Lifecycle records exist but are not linked into one chain | Escalation → ApprovalRequest → AuditEvent → ProcurementPoDraft exist independently | D: read-only chain assembler reusing existing records — NO new lifecycle infra |
| Governed period comparison already exists | `compare_metric_periods` (metrics.py) computes two periods deterministically | B: measurement REUSES it — realized impact is a governed delta, never LLM-computed |

Foreign-session hazard: 150 dirty files incl. `metric_bootstrap.py`,
`enterprise_data_tools.py` — never touched or committed.

## 1. Workstream B — Outcome Measurement

### B.1 Model — `outcome_measurements` (migration `113_outcome_measurements`, RLS)

Additive; `TimestampedBase` provides `id/org_id/app_id/is_deleted/created_date/...`.

- `approval_id` (String36, nullable, indexed) — the decision's authorization
- `action_id` (String36, nullable) — ontology action that executed
- `escalation_id` (String36, nullable) — originating situation
- `draft_id` (String36, nullable) — PO draft whose execution is being measured
- `metric_definition_id` (String36, nullable=False) — governed metric; MUST
  resolve to a published+enabled MetricDefinition in this org at record time
- `metric_name` (String255, nullable=False) — denormalized for display
- `direction` (String10, nullable=False, `increase|decrease`) — business
  expectation declared by the human (margin: increase; cost: decrease)
- `expected_impact` (Float, nullable) — declared, never computed
- `baseline_value` (Float, nullable) — governed compute at record time
- `observed_value` (Float, nullable) — governed compute at measure time
- `baseline_period` / `observed_period` (String20, nullable=False)
- `unit` (String30, nullable) — from the metric definition
- `realized_impact` (Float, nullable) — computed ONLY by the service:
  increase → observed − baseline; decrease → baseline − observed
- `measurement_status` (`pending|measured|failed`), `measurement_note` (Text)
- `verification_status` (`unverified|verified|rejected`),
  `verified_by`, `verified_at`, `verification_note`
- `evidence_refs` (JSON) — metric provenance, draft/ERP refs, documents
- `confounder_note` (Text) — fixed honesty note: attribution is not automatic

Unique: `uq_outcome_decision_metric = (org_id, approval_id, metric_definition_id,
baseline_period, observed_period)` — one measurement per decision+metric+period
pair (idempotent re-measure updates the same row).

### B.2 Service — `app/services/ontology/outcomes.py`

- `record_outcome(db, org_id, *, approval_id, action_id, escalation_id,
  draft_id, metric_ref, direction, baseline_period, observed_period,
  expected_impact)` → validates the metric (published+enabled, org-scoped),
  computes `baseline_value` via `compute_metric(filters={time_col: baseline_period})`
  (typed-unavailable propagated honestly as status `failed` if the baseline
  cannot be computed), stores status `pending`.
- `measure_outcome(db, org_id, measurement_id, *, measured_by=None)` →
  recomputes the governed metric for `observed_period`; refuses when the
  measurement is already `verified` (frozen), computes `realized_impact`,
  attaches `evidence_refs` = {metric provenance incl. execution_trace/SQL,
  periods, draft/ERP ids}. Confounders: the note states the delta is
  period-over-period and MAY include non-action effects — attribution is the
  reviewer's call, not automatic.
- `verify_outcome(db, org_id, measurement_id, *, verified_by, decision,
  evidence_refs=None)` → org-admin only (enforced at router). Requires
  `measurement_status == "measured"`. Sets verified + audit event
  `outcome_verified`.
- `list_outcomes(db, org_id)` → with metric/draft/ERP references.

Semantics (B3): realized = observed − baseline (increase) or baseline −
observed (decrease). Periods must differ and both be computable through the
SAME MetricDefinition (compatible population/unit by construction — the
governed compute path guarantees the same base table/column/aggregation).
LLM may explain; it can never calculate or certify.

### B.3 Router (additive to `ontology.py`)

- `POST /ontology/outcomes` — org-admin (declaring expected impact is a
  governance act)
- `POST /ontology/outcomes/{id}/measure` — authenticated org member
- `POST /ontology/outcomes/{id}/verify` — org-admin ONLY
- `GET  /ontology/outcomes` — authenticated org member

## 2. Workstream C — Connector-neutral ERP write-back (sandbox)

### C.1 Contract — `app/services/ontology/erp.py`

`ERPAdapter` protocol: `submit_purchase_order(payload: dict) -> dict`
returning `{external_ref, status, adapter: connector_id}`.
Adapter registry keyed by `connector_id` (deterministic; sandbox built-in).

`SandboxErpAdapter`: validates the payload (supplier, ≥1 item/amount),
returns a deterministic `external_ref = f"SANDBOX-PO-{counter}"` — a real
in-adapter ledger (module-level dict) proving the write landed, so E2E can
assert the external system actually received the order.

### C.2 Ledger — `erp_submissions` (same migration 113, RLS)

`org_id, draft_id, connector_id, idempotency_key, status
(executing|succeeded|failed), external_ref, request_json, response_json,
error_message, approval_id`.
Unique: `(org_id, idempotency_key)` — duplicate execution is impossible;
a retry with the same key returns the existing record (no double submit).

### C.3 Tool — `erp_submit_po` (mirrors `create_po_draft` exactly)

Registered in the tool registry with the DIRECT-CALL GUARD (refused without
`context.approval_id` — no LLM-initiated ERP writes). Params:
`draft_id` (required, must exist in org), `connector_id` (default `sandbox`).
Execution: validates draft → builds idempotency key
`sha256(approval_id|draft_id|connector_id)` → creates/loads ledger row
status `executing` → calls the adapter → records result + `external_ref` →
status `succeeded`/`failed`. Adapter exceptions surface as honest failures
(retry re-enters idempotently). Lifecycle:
`proposed → awaiting_approval (ApprovalRequest) → approved → executing →
succeeded | failed`.

### C.4 Security (all pre-existing mechanisms, reused not bypassed)

Write runs ONLY through `POST /ontology/actions/{action_id}/execute-approved`
→ `check_action_permission` (console_user) → approval identity/expiry/
param-match gate → tool guard → ledger idempotency. Every attempt and result
audited (`AuditEvent`: `erp_submission_succeeded|failed`, plus the existing
`permission_denied` path). No frontend→ERP calls; no LLM SQL writes.

### C.5 Honesty boundary

This is a SANDBOX adapter. Production ERP integration additionally requires:
real connector credentials/vault, network error taxonomy, retry/backoff
policy, vendor API mapping, reconciliation — documented in the module
docstring, not claimed as done.

## 3. Workstream D — Decision lifecycle chain (read-only assembler)

`GET /ontology/decisions/{decision_ref}/lifecycle` where `decision_ref` may
be an escalation, approval, or draft id. Walks EXISTING records and returns
ordered stages, each carrying references to the previous:

`problem (Escalation) → investigation (situation audit) → approval
(ApprovalRequest) → decision (resolve/execute audit) → execution
(ProcurementPoDraft + erp_submissions) → measurement (outcome_measurements)
→ verification (verified_by/at)`.

Missing stages are returned as `null` with a reason — never fabricated.
Cross-org refs are invisible (org filter on every query).

## 4. Frontend (Experience E upgrade + D hook)

- Outcome list: expected vs measured vs verified impact, status chips,
  measure/verify buttons (verify visible to admins only), evidence popover
  (provenance incl. SQL trace).
- Lifecycle timeline per decision from the chain endpoint.
- Honest empty/unavailable states unchanged.

## 5. Browser validation (Workstream A)

Playwright against the real running stack; screenshots of all six
experiences; scenarios per the master directive incl. blocked execution
without authorization and success with a valid approval.

## 6. Test plan

- `tests/test_ebom_outcomes.py`: record (baseline governed compute),
  measure (realized delta, direction both ways), re-measure idempotent,
  verify authorization (viewer 403, admin ok), verify requires measured,
  unavailable metric honest failure, cross-org isolation.
- `tests/test_erp_connector.py`: tool guard (direct call refused),
  execute-approved happy path (external_ref recorded), idempotency
  (duplicate key → single submission), adapter failure → honest failed
  status + retry, approval expiry/drift still enforced by the existing gate,
  cross-org draft refusal, audit rows present.
- `tests/test_ebom_lifecycle.py`: full chain assembly from the automotive
  scenario; missing-stage honesty; cross-org invisibility.
- Regression: existing EBOM + P0–P7 slice; frontend build.
