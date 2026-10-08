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
   `PLANDATE`, `CUST_NAME`, `Acme`, `huizhou`, `材料B`, `sample product`, `sample product`).
4. Approval gating on destructive operations.
5. Secret redaction / internal-plumbing hygiene (never recite harness internals).
6. Loop bounds themselves (a bound is a primitive; only the hardcoded VALUE moves
   into policy config).

## 3. Phases

### Status
- **Phase 0 — DONE** (commit bfe2178b): `rule_events.py` + `harness_rule_events`
  + 12 rules instrumented. Live rows confirm the refusal prompt records
  `is_file_deliverable_request fired=0` while the genuine ask records `fired=1
  forced=docx`, and the force rule records `iteration=9/10`.
- **Phase 1a — DONE**: `app/services/harness/policy.py`; the 10 budget literals in
  agents.py now resolve from policy. Defaults are byte-identical to the old
  literals (pinned by test), so this is a refactor with zero behaviour change.
  Overridable per field (`HARNESS_BUDGET_<FIELD>`) or per agent class
  (`HARNESS_BUDGETS_<CLASS>_JSON`); invalid/non-positive values are refused, so a
  bad env var can never produce an unbounded loop.
- **Phase 1b — DONE.** `app/services/permissions/approval.py` (new): tools
  declare their own risk at registration (`risk=`, `dangerous_actions=`),
  `resolve_risk()` resolves registry-first with a documented fallback table,
  `NEVER_GATE` keeps agent scratch state out of the gate, standing grants
  ("allow for this conversation") live in `conversation.metadata_`, and an
  approval is bound to the exact call by tool name + argument digest.
  Two security holes found and fixed while doing this:
  (a) the resume path escalated the **singleton** permission checker to
  `full_auto` for the duration of an `await` — a process-wide window in which
  every concurrent turn (other users included) skipped confirmations and
  plan-mode write blocks; replaced by the call-scoped approval context.
  (b) `_get_effective_config` swallowed authorization-lookup exceptions
  (`except Exception: pass`), degrading `allowed_tools` to "all allowed";
  now fails CLOSED and logs at ERROR.
  Also: `cronjob`/`kanban`/`update_env_config` deletions were ungated (none is
  in `WRITE_TOOLS`), and approval records said nothing about what was being
  approved. Correction to this document's earlier inventory: approval was
  **not** absent — the 3 modes, `ApprovalService`, `approval_requests` and the
  pause/resume path already existed; what was missing was the destructiveness
  concept and the call-scoped binding.
- **Phase 1c — DONE.** `app/services/harness/turn_recorder.py` (new): one
  `AgentRun` per chat turn (`mode="chat"`, `caller_context.conversation_id` so
  `GET /agent-runs/conversations/{id}` returns chat turns), one `AgentRunStep`
  per model call / tool call via the existing `CheckpointSink`. Fail-open and
  flag-gated (`HARNESS_CHAT_RUNS_ENABLED`) like Phase 0. An approval pause closes
  the run as `paused` and stores the `run_id` in `_resume_state`; the resumed
  turn is meant to continue that same run (the resume-path wiring itself is NOT
  done yet — the run stays `paused` after a resume, which is consistent, just
  not continued). `start_recorder()` / `now_ms()` / `preview_text()` are called
  from `agents.py:add_message` at 4 points: run creation, per model call, per
  tool call, and completion.
  **Schema drift found and repaired:** the live `agent_run_steps.id` is an
  integer PK (`nextval('agent_run_steps_id_seq')`) while the model declares
  `VARCHAR(36)`. Every `AgentRunStep` insert therefore failed with
  `InvalidTextRepresentation` — the ENTIRE run spine's per-call persistence was
  dead on this database, not just the chat path. The table had 0 rows, so it was
  dropped and recreated from the model metadata (no data lost).
- **Phase 1d — DONE.** `app/services/harness/modes.py` (new): mode is
  `execute | plan`, stored in `conversation.metadata_["agent_mode"]` — the same
  convention as `permission_mode`, so there is one way to express "a setting for
  this conversation". It **authors nothing**: the plan is the model's own
  `AgentTodo` list (`plan_from_todos` reads it back for consumers of the old
  `TurnPlan`). Enforcement lives in `execute_tool`, ordered deny → plan-mode
  proposal → approval gate: reads run, while write / destructive / externally
  visible calls come back as `plan_mode: true` proposals with a message telling
  the model to record its plan via `todo`. A proposal is never retryable, or the
  loop would burn the turn's iteration budget re-issuing it. `mode_get` /
  `mode_set` tools (`tool_handlers/mode_tool.py`) give the model ownership of its
  own mode, and both are in `NEVER_GATE` — gating them would trap the model in
  plan mode with no way back to execute. Not yet done, deliberately:
  `turn_planner` still runs alongside (Phase 2 is shadow, Phase 3 deletes it),
  and `mode_to_system_block()` is exposed but not injected, because the
  per-call proposal already teaches the mode.
- **Phase 2 — DONE.** `app/services/harness/shadow.py` (new): compares what each
  rule decided against what the model actually did, read from the turn's own
  tool-call trace, and stamps `diverged: true` + both decisions into the Phase 0
  row's `detail` (the field the model already reserved for it). One hook in
  `rule_events.record_rule_event` computes it, so **none of the six rule modules
  changed**. The trace is fed from `agent_tools.execute_tool` *before* any gate, so
  a call the harness blocked still counts as the model's own judgment; it is a
  contextvar, bounded at 200 entries, and never touches the DB on the caller's
  thread.
  `bind_turn` is now actually called (agents.py:7008, the one place a chat tool
  loop starts). Phase 0's acceptance criterion — "a row with … conversation id" —
  was never met in production: nothing called `bind_turn`, so every live row
  carried `conversation_id = NULL`. Phase 2's evidence is grouped by turn, so this
  had to be fixed, not worked around.
  `turn_planner.build_turn_plan` is instrumented too (it was the one rule in the
  Phase 3 deletion list with no Phase 0 coverage, so Phase 3 step 2 would have had
  nothing to delete on).
  **What the measurement can and cannot say.** The three force guards already
  return `False` when the artifact exists (`if file_artifact_created(...)`), so for
  them a fire proves only that nothing existed *at that moment* — `rule_redundant`
  is unreachable by construction. `finish_turn()` (called after the tool loop)
  resolves each such fire against the final trace:
  `rule_preceded_delivery` (the deliverable appeared afterwards — consistent with
  the rule working; causation is **not** observable and is not claimed) vs
  `rule_fired_no_delivery` (the turn still shipped nothing — the rule did not
  achieve its stated aim). No counterfactual replay is attempted anywhere: a trace
  cannot prove what the model *would* have done with the rule silent, and the
  labels are named so no unproven claim enters the evidence.
  Verdicts: `keep_but_ineffective` (fires and nothing ever follows → Phase 4's
  bounded evaluator, not a deletion), `keep_load_bearing`, `delete_candidate`
  (≥ `MIN_SAMPLE=20` evaluations, never load-bearing), `insufficient_evidence`.
  Drift guard: a test scans the source tree for `@observe_rule` and fails if any
  instrumented rule has no declared primitive.
  Live-probed in `zhanlu-copy-backend` on a real conversation: all four cases
  (fire/no-deliver, fire/then-deliver, delivered-before-the-rule, turn_planner)
  wrote rows to `harness_rule_events` with `conversation_id` populated.
- **Phase 3/4 — TODO.** Phase 3 still needs `divergence_report()` rows to
  accumulate over real turns; that is elapsed usage, not effort.

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

### Phase 2 — shadow (this commit)
`app/services/harness/shadow.py`: the turn's tool-call trace is the primitive the
forcing rules are compared against. Rule firing and model action are recorded
together on the Phase 0 row (`detail.diverged`, `detail.agreement`,
`detail.primitive.model_acted`); `finish_turn()` resolves unresolved fires against
the final trace; `divergence_report()` is the Phase 3 shortlist. Fail-open, cheap,
flag-gated (`HARNESS_SHADOW_ENABLED`).

Acceptance: every rule's row carries both decisions and a divergence label; the
three force guards resolve to delivery/no-delivery at turn end; a rule that ever
supplied an outcome the model had not reached is never a deletion candidate; zero
behaviour change; no rule module edits.

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
