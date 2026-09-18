# Agent Harness migration — remove hardcoded rules, keep only primitives

Date: 2026-09-18 · Checkout: `zhanlu_n8n_main_version` ("this project", :5012/:8090)
Goal: the turn runtime stops deciding FOR the model. Rules that pre-empt the
model's judgment are replaced by harness primitives, then deleted on evidence.

## 0. Why (the trigger)

A live turn proved the point: the user wrote `直接输出，不要输出 文档` and the
platform forced a `create_artifact` near the iteration cap, because
`is_file_deliverable_request()` read the prohibition as a request. The immediate
fix (`is_deliverable_negated`, commit d3e5de96) made the RULE smarter. That is
backwards: the rule should not exist. This document is the plan to delete the
whole family after installing what it compensates for.

## 1. Corrected inventory (measured 2026-09-18 — first pass was wrong)

Two capabilities I first reported as ABSENT already exist, and are wired:

| Capability | Reality | Evidence |
|---|---|---|
| Compaction | **PRESENT + WIRED** (2,003 lines) | `app/services/compaction/` (token_estimator, pre_api_prune, microcompact, full_compact, context_collapse, session_memory); called from agents.py:123, :7084 `auto_compact_if_needed`, `get_context_window` |
| Harness run spine | **PRESENT, partial use** (1,391 lines) | `app/services/harness/` AgentRunOrchestrator + AgentRunService + checkpoint/tracing sinks; used by `routers/agent_runs.py`, `services/swarm/runtime.py:276`, `services/research/loop.py` — **NOT by the chat turn path** |
| Model-facing todos | **PRESENT** (model + tool file) | `app/models/agent_todo.py` (per-conversation JSON item list), `app/services/tool_handlers/todo_tool.py` |
| Iteration limits | PRESENT but hardcoded | agents.py:250 `MAX_TOOL_ITERATIONS=40`, :259 `TOOL_CALL_HARD_CAP=10` |
| OpenTelemetry | PRESENT | `app/services/tracing/{otel,genai,metrics,spans}.py` |
| Skills / delegation / web | PRESENT | Toolkit+skill_routing; delegation (47 files); web_search_tool.py |
| Tool approval | **ABSENT** | 0 hits for `approval_mode` / `require_approval` / `needs_approval` repo-wide |
| Agent modes (plan/execute) | **ABSENT** | no mode concept; `turn_planner.py` is a server-authored plan instead |
| Per-service-call history | PARTIAL | checkpoints exist (`harness/checkpoint_sink.py`) but not per model call in the chat loop |

### The hardcoding debt (the actual target)

- **3,106 lines** across 6 rule modules: `file_turn_guard` (489),
  `dashboard_turn_guard` (830), `pptx_turn_guard` (177), `turn_planner` (441),
  `goal_contract` (709), `synexia/intent_router` (460).
- **59 compiled regexes**: agents.py 25, goal_contract 17, intent_router 9,
  dashboard_turn_guard 4, file_turn_guard 2, turn_planner 2.
- **12 deterministic decision functions** that read the user's text and decide
  agent behaviour:

| Rule | File:line | Forces |
|---|---|---|
| `should_force_create_dashboard` | dashboard_turn_guard.py:198 | create_fullstack_dashboard |
| `is_live_dashboard_request` | dashboard_turn_guard.py:142 | — (classifier) |
| `should_force_create_file` | file_turn_guard.py:295 | create_artifact |
| `is_file_deliverable_request` | file_turn_guard.py:138 | — (classifier) |
| `should_force_create_pptx` | pptx_turn_guard.py:84 | pptx deck build |
| `is_pptx_request` | pptx_turn_guard.py:48 | — (classifier) |
| `detect_file_intent` | synexia/intent_router.py:266 | skill routing |
| `is_file_read_request` | synexia/intent_router.py:159 | suppresses creation |
| `detect_deck_edit_intent` | synexia/intent_router.py:446 | deck-edit skill |
| `detect_soft_intent` | synexia/default_skills.py:133 | skill injection |
| `is_online_research_request` | turn_action.py:208 | research path |
| `is_report_request` | goal_contract.py:61 | report directives |

Call sites (all in `app/routers/agents.py`, three turn paths — add_message,
resume_conversation, add_message_stream): 73, 99, 103, 104, 198, 800, 6880,
6986, 7013, 7035, 8587, 8604, 12592, 13342, 13403, 13428.

- **Post-loop rewriting** (hardcoded proxies for "is this answer good"):
  claim-audit span deletion, `_GENERIC_EMPTY_CONTENT_FALLBACK`, `INCOMPLETE_TURN`,
  the empty-answer net (`agents.py:292 _NON_ANSWER_MAX_CHARS=600`,
  `:1126 _WEAK_CONTENT_MAX_CHARS=160`, `:1127 _WEAK_CONTENT_SYNTH_MIN_CHARS=400`).

## 2. Invariants that are NOT hardcoding and stay

Authorization is not judgment. Deleting these is a security regression:

1. Auth / tenant / org / app scope resolution.
2. `bound_kb_ids` — the user-facing agent must physically not reach an unbound KB.
3. Zero-domain-tokens in core (grep CI: `erp_`, `FAMOUNT`, `FALLAMOUNT`,
   `PLANDATE`, `CUST_NAME`, `Ecisco`, `huizhou`, `双环戊二烯`, `C5`, `C9`).
4. Approval gating on destructive operations.
5. Secret redaction / internal-plumbing hygiene (never recite harness internals).
6. Loop bounds themselves (a bound is a primitive; only the hardcoded VALUE moves
   into policy config).

## 3. Phases

### Phase 0 — instrument (this commit)
`app/services/harness/rule_events.py`: contextvar turn correlation, in-memory
counters, durable `harness_rule_events` rows, `@observe_rule(...)` decorator that
is **fail-open** (a telemetry failure must never change a turn). Decorators land on
the 12 decision functions in their own modules — no new logic, no agents.py
changes. Evidence query is psql on `harness_rule_events`.

Acceptance: every rule fires a row with rule name, decision, forced target,
conversation id; zero behaviour change; unit tests pin the fail-open contract.

### Phase 1 — install the missing primitives (no deletions)
- 1a **Harness policy object**: one place for iteration/wall-clock/token budgets
  (replaces the ~8 constants in agents.py), overridable per agent class.
- 1b **Tool approval**: `approval_mode` on destructive tools + standing approvals
  + a rules hook. First primitive that lets us STOP pre-emptively blocking.
- 1c **Per-call history persistence**: persist after each model call inside a tool
  loop (reuse `harness/checkpoint_sink.py`); chat path joins the run spine.
- 1d **Modes**: plan/execute as a provider, replacing `turn_planner`'s
  server-authored plan; expose `mode_get`/`mode_set`; reuse `AgentTodo`.

### Phase 2 — shadow
Primitive live, rule still live, divergence logged (`diverged: true` rows in
`harness_rule_events` with both decisions). One full cycle before any deletion.

### Phase 3 — delete, in this order (each needs ~0 divergence over N live turns)
1. Force-nudges: file → dashboard → pptx (the family that produced the live bug).
2. `turn_planner` heuristics.
3. Post-loop answer rewriting (claim-gate span deletion → evaluator input).
4. Guard retries / char caps.

### Phase 4 — bounded evaluator loop
Post-loop rewriting becomes a bounded loop with an evaluator (predicate or judge)
so the MODEL repairs its own draft, inside the iteration/wall budget from 1a.

## 4. Rollback / safety

Every phase is flag-gated (`HARNESS_RULE_EVENTS_ENABLED`, then per-primitive
flags). Phase 3 deletions are one revert commit each, ordered so the most
user-visible rule goes first. No phase deletes a rule before its primitive has
been live for one full cycle.

## 5. Verification discipline

- Unit tests per module; a live probe for each primitive (in-container).
- Live E2E turn per phase, read back the persisted turn (not the answer text).
- Commit per phase; keep the working tree disciplined (foreign WIP untouched —
  this tree is edited concurrently).
