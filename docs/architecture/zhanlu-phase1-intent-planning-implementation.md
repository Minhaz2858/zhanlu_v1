# Zhanlu Phase 1 — Intent Understanding, Dynamic Planning & Execution Strategy (implementation record)

**Date:** 2026-10-31 · Status: IMPLEMENTED + INTEGRATION-TESTED (deterministic planner-level
evidence; live LLM verification remains a separate acceptance gate — provider quota blocker).

## 1. Previous behavior (verified, not assumed)

- Intent understanding was **implicit**: the ReAct loop's tool choice + `build_turn_plan`'s
  mechanical precedence (dashboard > report > data > generic) — no persisted, semantic
  classification.
- The plan was `TurnPlan(kind, steps)` only — no objective, constraints, acceptance criteria,
  risk, approvals, verification requirements, or strategy.
- Strategy selection existed structurally (direct execution default; delegation broker-governed;
  DAG flag-gated) but was **invisible** — nothing observable recorded which strategy a turn used.
- Reviewer: `_review_and_maybe_reinvestigate` (delegate_tool) already ran the fact-safe
  reviewer and a BOUNDED targeted re-investigation on missing evidence — but the verdict→decision
  mapping was implicit and the parent LLM saw only the raw review dict.

## 2. Changes (all extend existing mechanisms — no new planner/orchestrator/registry)

| Item | Change | File |
|---|---|---|
| P1.1 Intent taxonomy | `INTENT_TYPES` (10 values), validated semantically by the PLANNER LLM inside the existing one-shot planning call — **no keyword router**; template path leaves `intent_type` empty (it cannot classify semantically) | `turn_planner.py` |
| P1.2 Plan enrichment | `TurnPlan` gains optional `intent_type`, `objective_statement`, `expected_outcome`, `constraints`, `acceptance_criteria`, `verification_requirements`, `risk_level` (low/medium/high/critical), `required_approvals`, `execution_strategy` — all default empty; `_norm_plan_meta` drops unknown/invalid values (never guesses) and wraps scalars | `turn_planner.py` |
| P1.1 persistence | `parse_dynamic_plan` extracts the meta fields from the planner JSON alongside steps (tolerant); `dynamic_plan_prompt` documents the fields + "never invent evidence or figures" | `turn_planner.py` |
| Backward compat | steps-only JSON, bare lists and numbered-list fallbacks parse exactly as before with empty meta; `build_turn_plan` gains an optional `plan_meta` kwarg | `turn_planner.py` |
| P1.3 Strategy | `execution_strategy` ∈ {direct_response, direct_tool, single_specialist, multi_agent}; template default by mechanical kind (`none→direct_response`, else `direct_tool`); LLM may classify richer strategies; **invalid strategy degrades to the conservative default**; delegation/DAG escalation paths unchanged (broker still governs) | `turn_planner.py` |
| P1.4 Reviewer→replan | `evaluate_reviewer_findings(review)` in the EXISTING replan policy module: structured decision (`should_replan`, `recommended_next_action` ∈ gather_missing_evidence / revise_response / resolve_contradictions / address_failed_criteria, gaps preserved **verbatim**); attached as `result["review_guidance"]` after the review pass (flag `REVIEWER_REPLAN_GUIDANCE_ENABLED`, default on; advisory only); the existing bounded re-investigation remains the active mechanism | `synexia/replan_policy.py`, `delegate_tool.py` |
| P1.5 Observability | `plan_meta` typed live event (persisted, scrubbed through the same content-invariant sanitizer) + `plan_meta_frame` SSE frame, emitted once when the plan has steps — **never emitted empty** (no fake progress); the authoritative-objective rule holds: meta travels ALONGSIDE the raw user message, never replacing it | `agents.py`, `turn_planner.py` |

## 3. Plan schema (v2 — all new fields optional)

```
TurnPlan {
  kind: none|dashboard|report|data|generic        // existing
  steps: [TurnStep]                                // existing
  intent_type?: <10-value taxonomy>                // semantic, LLM-classified
  objective_statement?: str(≤600)
  expected_outcome?: str(≤600)
  constraints?: [str]
  acceptance_criteria?: [str]                      // how completion is judged
  verification_requirements?: [str]                // evidence to check before the final answer
  risk_level?: low|medium|high|critical
  required_approvals?: bool
  execution_strategy?: direct_response|direct_tool|single_specialist|multi_agent
}
```

## 4. Strategy logic (honest scope)

The four strategies map onto the existing runtime: `direct_response` (no tools),
`direct_tool` (GA's own tools), `single_specialist` (one governed delegation),
`multi_agent` (heterogeneous batch / DAG / peer collaboration — Collaboration Broker-governed).
Selection remains the LLM's dynamic choice per turn (objective complexity, capabilities,
permissions, risk, budgets — the broker enforces the hard boundaries); Phase 1 makes the
selection **explicit and observable** rather than replacing the decision logic with rules.
Direct execution stays the default; escalation to specialists happens when the planner or the
loop's observations justify it.

## 5. Reviewer integration

`evaluate_reviewer_findings` decision table: approved+complete+no-gaps → no replan;
missing evidence (`needs_evidence` or `missing_evidence`) → `gather_missing_evidence`;
unsupported claims → `revise_response`; contradictions → `resolve_contradictions`;
failed acceptance criteria → `address_failed_criteria`; needs-revision with no structured
gaps → `revise_response`. Missing evidence is carried verbatim — the system names what is
absent (Scenario 7 honesty: profit without verified cost data is reported as a limitation,
never invented).

## 6. Test results (fresh, this session)

`tests/test_phase1_intent_planning.py` — **21 passed** (TDD: RED first — ImportError, then
4 expectation-driven failures caught a real NameError bug in `plan_meta_frame` and fixed the
invalid-strategy semantics to "degrade to safe default"): taxonomy completeness; enriched-meta
parse; invalid-value dropping; steps-only/numbered-list backward compatibility; template
strategy defaults; explicit meta application; `plan_meta()` emptiness; frame scrubbing
(identifier → `[data]`); prompt documentation; reviewer decision table (7 cases); scenarios
1/2/4/7 at planner level. Regression: turn_planner **43** · live_plan_persistence **3** ·
p1_orchestration **13** · p1p2_hybrid **12** · p2_phase2_collaboration **22** ·
delegate_task_endpoint **4** · p3_execution_tree **18** · live_events **53** ·
content_invariant_hardening **26** — all passed. Frontend production build: **exit 0**.

## 7. Remaining limitations

- Scenarios 3/5/6 are covered at the planning schema level (same parse/decision machinery);
  full end-to-end runs require a live provider (BLOCKED — quota). Mocked/deterministic
  evidence is integration-level, NOT live-verified.
- `intent_type` on the template path stays empty by design (semantic classification is the
  planner LLM's job; a keyword fallback would violate the no-keyword-router rule).
- `required_approvals` is representational — approval ENFORCEMENT remains the existing
  governance gates (broker risk tiers, `Execution.needs_approval`); the plan field informs
  them and the UI, it does not bypass anything.
- `plan_meta` UI rendering (intent/strategy chips) is a small frontend follow-up; the event
  is persisted and reload-safe already.

## 7b. Phase 1.5 — completion record (2026-10-31)

Closed the four Phase-1 gaps (RED→GREEN, TDD; `tests/test_phase15_completion.py` — 18 tests):

1. **Intent consistency**: `unknown` added to `INTENT_TYPES` (first-class value — an
   unclassified turn is represented, never omitted). Template path: greeting/kind-none →
   `conversational`; every other template kind → `unknown`. LLM plans omitting intent →
   `unknown`. `plan_meta()` therefore ALWAYS carries `intent_type`. Explicit planner
   values still win (`apply_meta` runs after defaults). Planner prompt documents `unknown`.
2. **Reviewer-guidance execution verified**: deterministic tests prove the bounded loop —
   one follow-up round, ≤2 gaps, follow-up context has `_enable_review` popped (no review
   loops, no duplicate delegation without new evidence), one re-review whose verdict wins,
   `review_guidance` attached. Plus Gap 2 enabler: `plan_to_system_block` now injects the
   plan CONTRACT (objective, constraints, acceptance criteria, verification requirements,
   risk, strategy) as compact key:value rows — the loop can finally be held to criteria,
   and reviewer findings have something concrete to check against.
3. **plan_meta UI (smallest useful)**: `LiveActivityStream` renders ONE compact row per
   turn (`Target` icon, violet): `Plan · investigation · multi agent · medium risk · 2
   criteria`. Language-aware (zh: `条标准`/`需审批`). Structured keys only — free-text
   fields (objective_statement etc.) are deliberately NOT rendered (no chain-of-thought
   in the feed). A later richer emission replaces the earlier row (no stacking).
4. **Strategy/actual consistency**: `plan_dag` added to `EXECUTION_STRATEGIES` (validator +
   prompt). Delegate results now carry `actual_execution_strategy` — `single_specialist`
   on single-task results, `multi_agent` on batch results — so recorded `plan_meta`
   claims are checkable against what really ran.

Regression: phase15 **18** + phase1 **21** (updated: exact-set taxonomy assertions, None
→ `unknown` expectations, greeting meta now carries intent) · turn_planner 43 ·
replan_policy 13 (2 pre-existing failures from foreign uncommitted "2B-4" `node_id` WIP,
unrelated) · live_events 53 · content_invariant_hardening 26 · p3_execution_tree 18 ·
multiagent_mas · p1_orchestration · p1_scenarios · p1p2_hybrid(+hardening) ·
delegate_agentapp_aware · delegate_task_endpoint · plan_step_added_event ·
plan_dag_forced_skill · live_plan_persistence · fsm_plan_summary_event · parse_plan_steps ·
plan_memory · tool_timeouts_p1 — all green except one pre-existing reflexion-flag failure
(foreign `config.py` WIP). Frontend: LiveActivityStream tests **7** green; full suite has
8 pre-existing failures in foreign-WIP files (Sidebar, steer wiring, ProjectDetail,
translations `verify_failed` key, previewToken) — none touch Phase 1.5 code. Production
build **exit 0**.

Rollback additions: revert the `unknown`/`plan_dag` taxonomy lines, the three defaulting
blocks in `build_turn_plan`/`parse_dynamic_plan`, the `plan_to_system_block` meta block,
the two `actual_execution_strategy` lines in `delegate_tool.py`, and the
`plan_meta` branch/icon/`planMetaLabel` in `LiveActivityStream.jsx`.

## 8. Rollback

| Change | Rollback |
|---|---|
| Plan meta fields + taxonomy | Revert `TurnPlan`/`_norm_plan_meta`/`INTENT_TYPES` in `turn_planner.py` (purely additive; callers unaffected) |
| Planner prompt enrichment | Restore the original prompt string in `dynamic_plan_prompt` |
| `plan_meta` event | Remove the two emission lines in `agents.py` (~14424) + `plan_meta_frame` |
| Reviewer guidance | Set `REVIEWER_REPLAN_GUIDANCE_ENABLED=false` (no code change needed) or revert the `delegate_tool` block |
| Tests | Delete `test_phase1_intent_planning.py` |
