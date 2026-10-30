# P0 Governance Hardening — Progress Log

**Status: COMPLETE** (2026-10-30). All tasks implemented, reviewed (spec + quality), regression-tested, live-smoke-verified.

## Actual results

| Task | Planned | Actual | Result |
|---|---|---|---|
| 1 · Audit vocabulary whitelist | test-first, whitelist `ontology` source + 3 event types | done as planned | RED→GREEN, commit `119ce4684` |
| 2–3 · Rule-matrix tests (RED) | 6 denial tests fail, 3 regression pins pass | exactly as predicted (6 FAILED / 3 PASSED) | — |
| 4–5 · `permissions.py` + chokepoint wiring | as planned | done; **schema discovery**: `agent_domain_bindings` has UNIQUE(org_id, agent_name, domain_id) → "multiple conflicting scopes" is structurally impossible; test replaced with uniqueness-guarantee pin | commits `69d19fcbb` |
| legacy test contract | predicted 2 core-suite failures | exactly `test_write_action_creates_governance_approval` + `test_execute_approved_action_full_contract` (empty context → now unidentified); updated to console identity | — |
| 6 · Approved-execution scope re-check | done | read-scope agent + valid approval → denied; console re-run → executes | ✓ |
| 7 · Router console marking | `caller_kind` added | also passes `authenticated_user_id` (refinement 1) | ✓ |
| 8 · Denial-audit + regression + smoke | done | see below | ✓ |

## Review outcome (mandatory spec + quality review)

Fresh reviewer verdict: REQUEST_CHANGES → **all findings fixed** in commit `c332250fc`:

1. **(blocking, fixed)** Identity claims (`caller_kind`/`authenticated_user_id`) were forwarded to bound tools via nested `execute_tool` context. A read action bound to `execute_ontology_action` could have inherited the console claim and fired a nested WRITE with no approval. Fix: claims stripped from forwarded context on BOTH paths; nested ontology writes re-enter the gate unidentified → fail closed. Regression test added (asserts the claims are absent in nested context and the nested write is denied).
2. **(fixed)** `execute-approved` now refuses param drift: runtime params absent from / differing with the approved params → honest refusal ("approval does not cover changed params"). An approval is proof for ONE specific write including its params.
3. **(fixed)** Audit helper: `db.get_bind()` moved inside the guarded region (detached session can't break the denial path); StaticPool isolation caveat documented; the failure-safety test now exercises the REAL wiring (AuditEvent model patched to raise at construction → refusal still returns cleanly).
4. **(fixed)** `ontology_tool._org_and_agent` masquerade default (`general_assistant`) removed.
5. **(fixed)** Vocabulary test covers `policy_violation`; added agent-path read-action regression test.

## Verification evidence

- **Unit/regression:** full ontology suite **74 passed, 0 failed** (`test_ontology_core` 13, `test_ontology_p0_governance` 15, `test_ontology_p1_batch` 13, `test_ontology_sensing` 10, `test_p3_batch2` 9, `test_p3_batch3` 8, `test_escalations` 6).
- **Live smoke (zhanlu-backend container, live-mounted code, restarted):**
  - Console path: `POST /api/ontology/actions/{id}/execute` (authenticated admin) → `blocked: confirmation_required`, approval `fc899b72…` created — human behavior intact.
  - Agent path (unbound agent, executed in-container against the live DB): `denied: True` — "agent 'unbound_smoke_agent' has no binding on this domain — write actions are refused (fail closed)".
  - Denial audit persisted live: `policy_violation / ontology / denied`, `caller_kind=agent`, `action=po_draft`.
- **Commits:** `119ce4684` (vocab), `69d19fcbb` (enforcement), `c332250fc` (review fixes).

## Deviations from plan

- Test for "multiple bindings any-write-grants" replaced: the UNIQUE(org_id, agent_name, domain_id) constraint makes conflicting scopes impossible; kept defensive any-write logic in the checker + a constraint regression pin.
- Console-user tests carry `authenticated_user_id` (refinement 1), making them stricter than originally drafted.

## Security posture after P0

- Writes: fail closed for unbound agents, read-only agents, unidentified callers (incl. MCP), and impersonated console claims.
- Reads: unchanged (`read=auto`).
- Approval flow for authenticated humans: unchanged, now with params immutability.
- RLS / org isolation: untouched; all new queries filter `org_id` + `is_deleted` per codebase convention.

**P1 (evidence taxonomy) not started** — per instruction, only after P0 review acceptance.
