# Zhanlu Phase 3.5 — Autonomous Recovery Completion & Credit-Efficient Validation

(2026-10-31. Constraint: ~90 LLM credits remaining → ZERO credits spent:
every test in this phase is deterministic; the only live call ever made
remains the single Phase-3 probe.)

## 1. What already existed (verified, reused — not rebuilt)

- Durable state: PlanNode (status/result/deps/requires_confirmation/risk_tier),
  ObservationRecord (incl. Phase-3 `evidence_ids`), AgentRunStep checkpoints,
  Execution tree, AgentInvocation two-phase lifecycle.
- Recovery: `recover_orphaned_invocations` (invocation level, 3600s, scheduled
  every 600s), `durable_execution.resume_decision` (Phase 3, node level —
  unconsumed until now), governance-store slots + Postgres advisory-lock
  single-worker scheduler guarantee.
- Semantic matching (Phase 3), multilingual markers (Phase 3), golden-eval
  runner (`golden_eval_runner.run_golden_suite`, mockable end-to-end),
  provider diagnostics primitives (Phase-3 probe).

## 2. What was missing → implemented

### P1 — Automatic task resumption (the Phase-3 gap)
`app/services/recovery_resumer.py` (NEW) — `recover_interrupted_plans(db)`:
a scheduled pass over persisted PlanNodes that CONSUMES
`resume_decision`:
- completed/skipped → never touched (`completed_preserved` counter)
- running within TTL (1800s) → left running (live worker owns it)
- stale read-only running → `pending` (re-eligible under idempotency key)
- stale approval-gated → `pending` AT THE GATE (flag intact; recovery never
  bypasses approval; never executed)
- stale destructive (risk_tier=high) → honest `failed` ("consequential write
  state unknown — manual verification required") — never auto-repeated
- stale with unmet dependencies → un-stuck to `pending` + `blocked_dependencies`
  audit (executor re-applies dependency logic; never counted as recovered)
- recently-failed transient read-only → `pending` (bounded retry; window =
  2×TTL; permission failures stay failed)
- EVERY action → audit `ObservationRecord(observation_type="recovery")` with
  action/reason/idempotency_key — visible after reconnection
- NEVER executes tools, NEVER creates AgentInvocation rows, idempotent across
  passes

Registered in `scheduled_tasks.py`: `PLAN_NODE_RECOVERY_INTERVAL = 1800` +
`PLAN_NODE_RECOVERY_ENABLED` settings gate + single-worker advisory lock
(reused). No new queue, no second scheduler.

### P2 — Execution-consistency audit (fixes folded into P1)
Verified against the models: a node CAN go completed while the invocation
runs (by design — H1 terminal update is first-terminal-wins at invocation
level); recovered nodes CANNOT duplicate invocations (the pass creates none —
tested); a parent cannot finish before required children (delegate_tool
awaits; H1 suites); evidence refs survive recovery (migration 115 column +
test); terminal status cannot regress (first-terminal-wins, existing suite);
interrupted tasks no longer remain permanently active (the new pass un-sticks
them — the exact P2 question); recovery events are persisted and queryable
after reconnection (tested). No redundant state fields added.

### P3 — Semantic selection deterministic scenario eval
`test_semantic_selection_scenarios.py`: 10 scenario families with a
deterministic fake embedder. One genuine defect found & fixed: none (matcher
behavior correct — earlier failures were fake-embedder artifacts). Verified:
department wall, publication gate, false-positive guard (translate task → []),
false-negative guard (paraphrase), GA-prefers-own-tools (conversational → []).

### P4 — zh/en equivalence focused audit
`test_multilingual_paired_audit.py`: 9 paired cases + mixed-language +
localized-hint coverage. TWO GENUINE DEFECTS found & fixed in
`execution_recovery._MARKERS`:
- English: "do not have permission" / "no permission" / "not permitted" and
  "doesn't exist" (MySQL contraction) were unrecognized → unknown_error.
- Chinese: "没有权限" / "策略拒绝" / "被拒绝" / "权限拒绝" unrecognized.
Also documented: "Unknown column" → `resource_not_found` is the EXISTING
correct contract (discoverable via list/describe).

### P5 — Provider diagnostics (ZERO network calls)
`app/services/provider_diagnostics.py` + `scripts/provider_diagnostics.py`
(exit codes 0/1/2; JSON mode). Reports: configured / credential_present /
model_enabled / provider_selected / fallback consistency / last error
CATEGORY (sidecar `.provider_last_error.json`, written by the probe path —
category only, never messages or keys). **LIVE FINDING from the offline run:
the primary model `deepseek-v4.1-flash` is NOT among enabled llm_models
registry entries (active: Kimi K2, MiniMax M2) — a configuration
inconsistency that plausibly explains the Phase-3 HTTP 401.** Fix path:
enable/register the primary model or repoint `LLM_MODEL` to an enabled
entry, then run ONE probe.

### P6 — Credit-free acceptance suite
`test_manufacturing_acceptance.py`: the 10 manufacturing scenarios run the
real deterministic pipeline (planner intent → semantic selection → failure
classification → recovery decision → evidence validation → final decision)
with a mocked embedder. Golden-format compatibility proven: scenarios are
representable as AgentTestCase records (`_parse_case_payload` round-trip) so
`golden_eval_runner` can execute them live later — no second framework.
Clearly labeled: deterministic simulation, NOT model evaluation.

### P7 — Technical-debt audit (prioritized findings)
1. SECURITY: none new (tenant wall, approval gates, publication gates all
   re-verified green).
2. DATA INTEGRITY: `llm_models` registry vs `LLM_MODEL` drift (see P5) —
   action required before live validation.
3. EXECUTION CORRECTNESS: foreign uncommitted WIP ("2B-4" node_id change)
   leaves 2 stale test failures in test_replan_policy — owner must reconcile.
4. RECOVERY RELIABILITY: closed by P1.
5. EVIDENCE ACCURACY: closed (migration 115 + audit records).
6. UX: recovery events now visible in LiveActivityStream (Phase 2.8 rows).
7. PERFORMANCE: semantic matcher loads the local embedder lazily (~10s first
   call, LRU-cached) — acceptable; noted.
8. CONVENIENCE: provider diagnostics CLI added.

## 3. Tests executed (deterministic only)

New: test_auto_resumption 14 · test_provider_diagnostics 8 ·
test_semantic_selection_scenarios 14 · test_multilingual_paired_audit 13 ·
test_manufacturing_acceptance 14 · (63 new tests, all GREEN).

Regression sweep (43 suites, run ONCE): **571 passed**; 3 failures — ALL
pre-existing and stash-A/B-verified in Phase 3 (replan_policy ×2 foreign
"2B-4" WIP; parallel_tools ×1 cross-test schema pollution). Frontend build
exit 0 (no frontend changes this phase).

## 4. Rollback

Delete `recovery_resumer.py` + the scheduled_tasks registration block +
`provider_diagnostics.py` + script + the new test files; revert the two
marker-tuple additions in `execution_recovery.py` and the `now` param +
httpx top-level import in existing modules. Migration set unchanged (115
stands from Phase 3).

## 5. Provider blockers

- Configured key invalid (HTTP 401, Phase-3 probe) AND primary model not in
  the enabled registry (offline diagnostic). Live acceptance (Level 5) stays
  BLOCKED until: valid funded credential + registry consistency, then ONE
  probe, then the carefully-selected acceptance run.
