# FSM Adoption Pilot — Golden Eval Gate → Decision-Layer Shadow → % Canary → Controlled Automation → Rollback Drill → Freeze

Date: 2026-09-03
Status: Design (pending review — NO implementation until approved)
Component: Synexia cognitive core — chat routing (`backend/app/routers/agents.py`), FSM (`backend/app/services/synexia/fsm.py`, `chat_loop_adapter.py`, `eval_harness.py`, `eval_ci.py`), Execution model (`backend/app/models/execution.py`), config (`backend/app/config.py`)
Reference: user's mandated rollout sequence (shadow dispatch → golden eval → limited canary → controlled automation → rollback rehearsal → freeze)

## Problem

The SynexiaFSM (GOAL → CONTEXT → PLAN → GATE → ACT/OBSERVE → VERIFY → QUALITY_EVAL → FINALIZE) is substantially wired into both chat paths (v2 blocking `add_message`, v3 SSE `add_message_stream`) and the eval surface (`POST /executions` always runs the FSM). It carries the stronger gates the product promises: deterministic VERIFY always-on, an LLM rubric verifier (flag-OFF), a quality gate that holds back low-confidence artifacts (0.4 interactive / 0.6 automation), Tier-2 quality eval, reflexion, confidence + factors, report cards.

But adoption is binary and unproven:

1. **Dormant in the deployed stack.** Code default `SYNEXIA_FSM_ENABLED=True` (config.py:543); docker-compose pins the env var `False`, so the shipped fleet runs the legacy loop for *every* turn — trigger or not. Nothing proves the FSM is safe to enable there.
2. **No staged rollout.** `is_fsm_enabled()` is process-global. There is no per-conversation % rollout, so canary = fleet flip, and there is no gradual path.
3. **No shadow evidence.** When a turn routes to legacy (flag off, or simple-conversation bypass, or non-data intent), the FSM is never consulted, so we cannot compare what the FSM *would* have planned/gated/verified against what legacy actually delivered.
4. **Weak golden gate.** The golden corpus (`backend/tests/eval/golden_scenarios.json`) has exactly 4 scenarios, all FSM-surface quality checks run against `/executions`. There is no common-behavior comparison corpus (same turn through FSM vs legacy) and no CI regression gate between the two loops.
5. **Stronger gates are off or unmeasured.** `SYNEXIA_VERIFIER_LLM_ENABLED=False`; quality eval runs (default True) but its Tier-2 verdicts are not compared against legacy outcomes; no latency/cost budgets exist for turning the rubric on.

The goal of this spec is NOT to build new FSM machinery. It is to make adoption of the existing machinery **evidence-gated and reversible**, in the exact phase order the user mandates, with a mechanism, data artifact, and exit criterion at every phase.

## Goals / Non-goals

Goals:
- A golden-eval regression gate that runs the *same* scenarios through the FSM and the legacy loop and blocks promotion on FSM-vs-legacy regression (Stage 1, offline, load-bearing evidence).
- A decision-layer shadow that samples legacy-routed turns, runs the FSM through PLAN→GATE (+ pre-flight VERIFY) WITHOUT executing ACT tool nodes, records the outcome for offline comparison, and never touches the user's critical path (Stage 2).
- Deterministic per-conversation % rollout (conversation-hash buckets) with per-agent override, an explicit env kill-switch that outranks everything, and legacy always reachable (Stage 3).
- Controlled automation adoption using the existing 0.6 automation gate (Stage 4).
- A documented rollback drill and a freeze step that flips the fleet default with docs updated (Stage 5).

Non-goals (defer):
- UI admin page for rollout/shadow (an ops report script + flags suffice for the pilot).
- Automatic canary promotion (every promotion is a human phase-gate review of the report).
- New verifier rubric work — the pilot uses the existing deterministic + LLM-rubric machinery.
- Eval leaderboards / benchmark suite (eval_harness docstring already scopes that out).
- Routing FSM for pure greetings/single-shot non-data turns — those stay legacy by design in all stages.
- Any change to trigger semantics (`should_trigger_planning`, follow-up override, data-bound override) — unchanged; rollout only decides *which conversations may use the FSM*, triggers decide *which turns*.

## Current State (verified facts the design builds on)

Routing, v2 and v3 identical shape (agents.py ~6642–6711 v2, ~11577 v3):
1. `_plan_trigger = should_trigger_planning(user_content)` — heuristic classifier (default mode), fires at score ≥ 0.6 after a simple-conversation bypass.
2. Follow-up override: `is_followup_refinement` forces True.
3. Data-bound override: agent has bound knowledge bases OR conversation metadata has `data_source_id`, AND not `_is_non_data_intent` → forces True.
4. `if _plan_trigger and is_fsm_enabled():` run `SynexiaFSM(db).run(ExecutionRequest(...))`; any exception falls back to the legacy loop below (best-effort, never blocks chat).

`is_fsm_enabled()` (chat_loop_adapter.py:30): env `SYNEXIA_FSM_ENABLED` if set, else `settings.SYNEXIA_FSM_ENABLED` (default True). No per-conversation dimension.

FSM run: `ExecutionRequest` → creates an `execution` row (execution.py, `EXECUTION_MODES = ["dynamic","frozen"]`, table `executions`, cascades to Plan/PlanNode/ObservationRecord) → runs states → `ExecutionResult` (assistant_content, tool_calls, artifact_ids, confidence + factors, quality_gate dict, quality_eval dict, plan_summary, file_exports). Message rows already persist `fsm_state / fsm_execution_id / fsm_confidence / fsm_quality_gate / fsm_plan_summary` (agents.py ~11986) — so *per-turn routing and FSM outcome are already queryable in the messages table for canary analysis.*

Eval: `eval_harness.py` EvalRunner + graders (`contains`, `not_contains`, `artifact_created`, `confidence_at_least`, `quality_gate`/`quality_gate_passed`, `quality_eval_passed`, `completeness_at_least`, `reflexion_verdict`); scenario schema `{name, user_message, agent_name?, conversation_id?, expect}`; corpus `backend/tests/eval/golden_scenarios.json` (4 scenarios). `eval_ci.py` posts each scenario to `/executions` (executions.py:38 → always FSM) and returns a HarnessReport; CLI `--scenarios`, `--report`. So today the harness measures the FSM surface only — there is no legacy arm.

Schema migration convention: no alembic. New columns are added via SQLAlchemy inspector + idempotent `ALTER TABLE ADD COLUMN` at startup (see automation_dispatcher.py:141–239 pattern).

## Design

### Routing decision module (shared by v2, v3, automation)

New module `backend/app/services/synexia/routing.py` — single source of truth so shadow/rollout hooks live in one place instead of being duplicated in the 2000-line chat handlers.

```python
# Precedence (highest first):
# 1. SYNEXIA_FSM_ENABLED env explicitly set  -> global override (kill-switch / full-on). No sampling.
# 2. SYNEXIA_FSM_ROLLOUT_ENABLED=true        -> per-conversation bucket decision.
# 3. settings.SYNEXIA_FSM_ENABLED (default True) -> back-compat: full FSM for trigger turns.

def resolve_fsm_eligibility(*, conversation_id, agent_name=None) -> tuple[bool, str]:
    """(may_use_fsm, reason) — reason in {"env-on","env-off","rollout-bucket","rollout-off","settings"}.
    Deterministic: same conversation_id always maps to the same bucket."""

def route_turn(*, user_content, conversation, agent_app, conv_ctx) -> RoutingDecision
    # Runs the existing trigger chain (should_trigger_planning / follow-up / data-bound / non-data)
    # then ANDs with resolve_fsm_eligibility. Returns {loop: "fsm"|"legacy",
    # reason: str, trigger: PlanTrigger|None}.
```

- `route_turn` replaces the inline trigger block in both chat paths; the legacy loop below the FSM call becomes the fallback branch of one `if decision.loop == "fsm"` — identical behavior when nothing is configured (reason "settings", trigger unchanged).
- Bucket: `int(hashlib.sha1(conversation_id.encode()).hexdigest()[:8], 16) % 10000 < pct*100`. Stable per conversation → consistent agent behavior within a session.
- Eval-mode routing override (below) short-circuits for golden runs.

New config (config.py, all flag-OFF defaults so bit-for-bit current behavior unless opted in):
| Flag | Default | Meaning |
|---|---|---|
| `SYNEXIA_FSM_ROLLOUT_ENABLED` | `False` | Allow % rollout when env flag unset |
| `SYNEXIA_FSM_ROLLOUT_PCT` | `0.0` | Fleet % of conversations eligible (0–100) |
| `SYNEXIA_FSM_ROLLOUT_AGENTS` | `{}` | Per-agent override map `{agent_name: pct}` |
| `SYNEXIA_SHADOW_ENABLED` | `False` | Sample legacy-routed turns into decision-layer shadow |
| `SYNEXIA_SHADOW_SAMPLE_PCT` | `5.0` | % of legacy-routed turns sampled (0–100) |
| `SYNEXIA_SHADOW_MAX_PER_DAY` | `500` | Hard daily cap on shadow executions (cost control) |
| `SYNEXIA_EVAL_MODE` | `False` | Honours `X-Synexia-Routing` header (never in prod) |

### Stage 1 — Golden eval gate (offline, dual-arm)

Purpose: load-bearing correctness evidence before any live change; a CI regression gate between the two loops.

**Scenario schema extension** (backward compatible):
```json
{
  "name": "common_db_overview_no_fabrication",
  "user_message": "give me a database overview",
  "routing": "auto",              // NEW: "auto"|"fsm"|"legacy"
  "expect": { ...existing graders... },       // graded on the run of THIS scenario
  "expect_common": { ...graders... }          // NEW: graded in BOTH arms when --dual
}
```
Existing 4 scenarios keep working unchanged (`routing` absent ⇒ "fsm", the current `/executions` behavior).

**Corpus expansion** — `golden_scenarios.json` grows to ≥ 3 categories × ≥ 6 scenarios each, covering the turn classes FSM owns and the legacy-fleet classes the pilot must de-risk:
1. `fsm_quality_*` (FSM surface, `/executions`): plan DAG produced, confidence floor, quality_gate holds low-confidence artifacts, quality_eval/reflexion verdicts, completeness — existing graders.
2. `common_data_*` (data-bound turns: DB overview, KPI query, dashboard/report generation with pinned data source): artifact_created, contains/not_contains on real output, no fabricated success. `expect_common` graded in both arms.
3. `common_tool_*` (multi-step / tool turns: web research, file/doc generation, automation-adjacent requests): same common graders + `routing` mostly "auto" to exercise the real trigger path.

**Dual-arm runner** — `eval_ci.py` gains `--dual`:
- Common scenarios (`expect_common` present) run through the real v2 chat endpoint — `POST /apps/{app_id}/agents/conversations/v2/{conversation_id}/messages` (agents.py:6441 `add_message`) — TWICE: Arm A with header `X-Synexia-Routing: fsm`, Arm B with `X-Synexia-Routing: legacy`. Both arms exercise the real trigger chain + routing override + loop; only the loop differs. The server honours the header ONLY when `SYNEXIA_EVAL_MODE=true` (agents.py + executions.py read it) — production cannot be steered by a header.
- `fsm_quality_*` scenarios keep running against `POST /executions` (executions.py:38, always FSM) — no legacy arm; those graders are FSM-surface only.
- FSM-only graders are skipped in the legacy arm; the report shows per-scenario pass/fail per arm + category rollups; JSON at `--report` path.

**Gate (human + CI)** — pre-canary exit criterion:
- FSM common pass-rate ≥ legacy common pass-rate (no regression), AND
- FSM common pass-rate ≥ 0.9 absolute in every category, AND
- all `fsm_quality_*` scenarios still pass (internal quality floor).
- Wired into the existing targeted-pytest discipline as `backend/tests/test_eval_ci_dual.py` + a documented `backend/scripts/run_eval.py --dual` invocation; the report is the artifact shown at the phase gate.

### Stage 2 — Decision-layer shadow (live, sampled, side-effect-free)

Purpose: prove the FSM's PLAN → GATE → pre-flight VERIFY is sound on *real legacy traffic* without ever executing ACT tool nodes (no duplicate side effects, no delivered output, off the critical path).

**Where it hooks**: in `route_turn`/chat paths, when the turn resolves to **legacy** (env-off fleet, trigger bypass, or non-data), sample at `SYNEXIA_SHADOW_SAMPLE_PCT` (deterministic per conversation, capped per day). Non-data greetings are excluded from sampling — they carry no planning signal (YAGNI).

**Shadow run**: `ExecutionRequest` gains `shadow: bool = False`. In `SynexiaFSM.run`, when `shadow=True`:
- Proceed INIT → GOAL → CONTEXT → PLAN → GATE normally (task-spec parse, plan DAG, policy gate).
- ACT: do NOT execute capability-router nodes. Mark plan nodes `skipped` with `reason="shadow"`; record what WOULD have executed (node types/tools).
- VERIFY: run the deterministic pre-flight checks against the plan (the always-on validator set), no post-execution rubric (no output to grade).
- FINALIZE: skip artifact creation; produce a `shadow_verdict` JSON (`{plan_nodes, would_execute:[tools], preflight_issues:[...], would_hold_artifacts: bool|None}`).
- Persist: `execution` row with `mode="shadow"` (extend `EXECUTION_MODES` to `["dynamic","frozen","shadow"]`) + `shadow_verdict`; the row links conversation_id and user_message like a normal execution but is excluded from every user-facing execution list query (`mode != "shadow"`).
- Do NOT write any message row, emit no SSE events, no `on_state_change`.

**Off critical path**: sampling decision is synchronous but cheap (hash + flag read). The shadow FSM run itself is dispatched AFTER the legacy response is committed — FastAPI `BackgroundTasks` (v2), `asyncio.create_task` after the final SSE event or a post-response background task (v3). Any shadow exception is logged non-fatally; the user path never awaits it. Latency budget: shadow contributes 0 ms to p95 user latency by construction; its own LLM cost is bounded by the daily cap.

**Report**: new `backend/scripts/shadow_report.py` — aggregates `mode="shadow"` executions joined to their conversations/messages, computes: sample size vs cap, FSM exception rate, shadow/legacy plan divergence rate (plan node count/tool sets differ), preflight issue rate, would-hold-artifacts rate; emits JSON + an HTML report (repo `outputs/`-style, matching the user's HTML-report default).

**Exit criterion (≥ 500 shadow turns or ≥ 1 week)**: FSM exception rate < 5%; no user-visible impact (shadow never on the path — asserted by tests); divergence/issues reviewed and triaged into Stage 3 risk notes.

### Stage 3 — Limited canary (real FSM execution, % rollout)

Purpose: run the real FSM (with the LLM verifier ON) for a slice of live traffic, measured against the legacy control slice.

- Canary deployment = remove the `SYNEXIA_FSM_ENABLED=false` env override from the canary fleet; set `SYNEXIA_FSM_ROLLOUT_ENABLED=true`, `SYNEXIA_FSM_ROLLOUT_PCT=5`, `SYNEXIA_VERIFIER_LLM_ENABLED=true`. Trigger rules unchanged — simple chat/greetings stay legacy everywhere.
- Measurement (no new telemetry needed — the message row already carries `fsm_state / fsm_execution_id / fsm_confidence / fsm_quality_gate`; legacy turns have them null, forming the control):
  - Quality: artifact hold-rate under quality gate, quality_eval Tier-2 verdict distribution, reflexion verdicts.
  - Reliability: FSM exception → legacy fallback rate (< 0.5% gate).
  - UX proxy: repeat-turn / user-correction rate on FSM vs legacy turns; thumbs/downstream signals where present.
  - Cost/latency: tokens + LLM calls per 100 turns, p95 latency — FSM + verifier budget: p95 ≤ legacy + 40% and cost per 100 turns ≤ 2.5× legacy (measured in the report, tuned at the phase gate before raising %).
- Promotion ladder: 5% → 10% → 25% → 50%, each a human phase-gate review of the canary report; kill-switch at any moment = set `SYNEXIA_FSM_ENABLED=false` env on the fleet (outranks rollout per precedence).

### Stage 4 — Controlled automation

Purpose: unattended runs only adopt the FSM after interactive canary holds, and only under the strict 0.6 automation gate.

- Automation execution paths (`automation_dispatcher.py` + automation run endpoints) that construct `ExecutionRequest(is_automation=True)` start using `route_turn` so automation honors the same rollout config, but with automation-specific rules: automation enters FSM only when rollout pct ≥ 50 or an explicit `SYNEXIA_FSM_AUTOMATION_ENABLED` flag (default False, added at this stage) is on.
- The existing 0.6 automation quality-gate threshold already applies (config.py:590); assert it in the report.
- Exit criterion: 2 weeks of FSM automated runs with 0 shipped quality-gate violations and 0 silent failures.

### Stage 5 — Rollback drill + freeze

- Drill (before freeze, on the canary fleet): set env `SYNEXIA_FSM_ENABLED=false`, run the Stage-1 sentinel scenarios in legacy mode, verify the legacy pass rate matches baseline, generate a rollback impact report, restore canary config. Scripted as `backend/scripts/rollback_drill.py` so the drill is repeatable, not tribal knowledge.
- Freeze: fleet default becomes FSM-on — compose env override removed and `SYNEXIA_FSM_ROLLOUT_PCT=100` (or env `=true`); update the stale FSM claims in `PROJECT.md` / agent-harness doc; record the kill-switch (`SYNEXIA_FSM_ENABLED=false`) in the ops runbook.

## Error handling

- Every FSM/shadow/rollout code path keeps the existing best-effort contract: any exception → legacy loop, chat never blocked. Shadow exceptions are additionally non-fatal by construction (background task, logged).
- Header override honored only under `SYNEXIA_EVAL_MODE=true`; if eval mode is off and the header arrives, it is ignored (never an error).
- Shadow daily cap overflow → skip silently, log at debug (cost control is a hard cap, not a throttle).
- Execution model `mode` column: existing rows are `dynamic`/`frozen`; shadow rows never surface in user queries — every execution-list query adds `mode != "shadow"` (audit existing queries during implementation).

## Testing

- `test_routing.py` — precedence (env > rollout > settings), bucket determinism + stability across calls, per-agent override, trigger chain unchanged when all flags default.
- `test_shadow.py` — shadow ExecutionRequest skips ACT (no tool side effects, nodes `skipped`), writes `mode="shadow"` row + `shadow_verdict`, writes no message row, daily cap respected, exception swallowed.
- `test_eval_ci_dual.py` — dual-arm runner against a stub run_fn: FSM-only graders skipped in legacy arm; gate math (no-regression + floors) correct; report shape.
- `test_eval_mode_header.py` — `X-Synexia-Routing` honoured iff `SYNEXIA_EVAL_MODE=true`.
- Existing suites stay green: `test_chat_loop_adapter.py`, `test_fsm_default_flag.py`, `test_eval_harness.py`, `test_quality_gate.py`.
- RAM-tight Mac discipline: targeted pytest files only, never full suites; the dual-arm golden run is an explicit script invocation (needs a live backend), not a unit test.

## Data model changes

`Execution` (execution.py) additions — via the repo's inspector + idempotent `ALTER TABLE ADD COLUMN` startup pattern (automation_dispatcher.py convention):
- `mode` values extended with `"shadow"` (string column, no migration needed beyond validator/comment).
- `shadow_verdict` JSON column, nullable.
No other schema changes; message-row fsm columns already exist.

## Acceptance summary (phase gates)

| Phase | Mechanism | Artifact | Exit criterion |
|---|---|---|---|
| 1 Golden eval | dual-arm eval_ci + corpus | JSON report | FSM common ≥ legacy common, ≥0.9/category, quality floor holds |
| 2 Shadow | decision-layer, sampled | shadow_report HTML | ≥500 turns, FSM exc <5%, 0 user-visible impact |
| 3 Canary | rollout pct 5→50 + LLM verifier | canary report | fallback <0.5%, quality/cost/latency budgets met per step |
| 4 Automation | 0.6-gated FSM automation | report | 2 wks, 0 gate violations |
| 5 Rollback+freeze | drill script + flag flip | drill report + runbook | drill pass = legacy baseline; freeze with kill-switch documented |

Out of scope for this spec (separate specs): UI for rollout/shadow, auto-promotion, benchmark leaderboard, greeting-turn FSM.
