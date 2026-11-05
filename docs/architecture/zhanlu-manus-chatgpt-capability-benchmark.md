# Zhanlu Capability Benchmark — Manus / ChatGPT / IBM Agent Architecture References

**Date:** 2026-10-31 · **Scope:** Parts A–C of the "Enterprise AI Operating System" mission
(Parts D+ of the mission spec were truncated on delivery and are NOT covered here — awaiting the remainder).
**Method:** every reference below was fetched and read in full on 2026-10-31; Zhanlu findings come from a
same-day read-only code audit (file:line anchors). Zhanlu's branch carries large uncommitted WIP; the
audit reflects the working tree as-is.

**Honesty rules applied:**
- Manus/ChatGPT internals are NOT claimed unless public documentation states them.
- ChatGPT's agent mode described in the launch post (2025-07-17) is **retired** — OpenAI's own help page
  now says "ChatGPT agent is no longer available. Use ChatGPT Work … and cloud browser". Capabilities are
  therefore attributed to the *documented* launch/help text, marked legacy where superseded.
- The Manus homepage is marketing-level ("Less structure, more intelligence", slide/site/game/video/design
  generation, Wide Research, Slack integration, web/desktop/mobile apps, SSO, team plans, API). The **API v2
  docs** are the concrete public capability source.
- "Zhanlu current" = verified working-tree behavior; "VERIFIED" = confirmed by code inspection and/or this
  session's passing test runs.

---

## 1. Reference product capabilities (documented)

### IBM Think — agent architecture canon (6 articles, fetched 2026-10-31)
- **Multi-agent systems** — autonomous agents (plan/tool-call/reason); centralized vs decentralized
  networks; hierarchical / holonic / coalition / team structures; per-agent domain specialization;
  direct + environment-mediated communication; advantages (flexibility, scalability, specialization,
  performance) vs limitations (shared-model failure modes, coordination complexity, unpredictable behavior).
- **Multi-agent collaboration** — task decomposition (planner or reasoning LLM); role-based responsibility
  assignment (functions, permissions, objectives per role); shared memory / intermediate outputs as the
  shared environment (watsonx "Shared Context and Memory Store"); explicit message passing vs implicit
  environment modification (OpenAI Swarm "routines and handoffs"); sequential / concurrent / **dynamic**
  execution; conflict resolution; collaborative planning; **result aggregation** by an orchestrator/final agent.
- **ReAct agents** — Thought→Action→Observation loop; dynamic situational tool selection; iterative
  feedback loop; stopping conditions = **iteration cap or condition-based termination**; scratchpad
  reasoning; multi-agent ReAct = strong central model delegating to cheaper subsidiary agents; benefits:
  versatility, adaptability, explainability, grounded accuracy; ReAct vs function-calling trade-off.
- **Agentic reasoning** — reasoning engine drives planning + tool calling; enterprise grounding via RAG;
  strategy menu: conditional logic → heuristics (goal/utility-based search) → ReAct → **ReWOO**
  (Planner/Worker/Solver, no per-step observation) → **LATS** (MCTS + self-reflection, memory-stored) →
  multi-agent reasoning (hierarchical vs horizontal); challenges: compute cost, interpretability, scale.
- **Agentic workflows** — dynamic multi-step workflows vs RPA; clarification questions first; adaptive tool
  substitution on failure (Andrew Ng's Wikipedia-substitution anecdote); iteration + **log the solution for
  future cases**; escalate with a detailed report only after adaptive steps are exhausted; HITL feedback;
  multi-agent integration with shared learned information.
- **A2A protocol** — Google 2025 → Linux Foundation; client/server roles; **Agent Card** (JSON metadata:
  name, description, version, endpoint, modalities, auth requirements); discovery via card fetch; auth via
  OpenAPI-style schemes (API key / OAuth 2.0 / OIDC), remote agent owns authorization; task lifecycle
  `submitted → working → input-required → completed → failed`; messages with typed parts
  (Text/File/Data); artifacts, incrementally streamable; HTTPS + JSON-RPC 2.0; SSE streaming; long-running
  tasks; **push notifications to a client-supplied webhook**; agents treated as opaque.

### Manus (product benchmark)
- Homepage: autonomous agent positioning; generation surfaces (slides, websites, games, video, design,
  images, music); **Wide Research**; Slack integration; web/desktop/mobile apps; SSO; team plans; API.
- **API v2** (`open.manus.ai/docs/v2`): REST task creation/management; **full multi-turn conversation
  support** (follow-up messages + result retrieval); **projects with shared instructions applied
  automatically** to project tasks; file attachments (PDF/images/CSV+); **webhooks on completion or
  input-required**; **skills (built-in + custom)** and **custom agents** management; error taxonomy
  (unauthenticated / invalid_argument / not_found / permission_denied / rate_limited / internal); v1 deprecated.

### OpenAI (product + engineering benchmark)
- **ChatGPT agent launch post (legacy):** unified agent = Operator (visual browser) + Deep Research +
  conversation; tools auto-selected: visual browser, text browser, **terminal**, direct API access,
  **connectors** (Gmail/GitHub); all on a **virtual computer** preserving context across tools; **browser
  takeover** for private logins (inputs not captured); editable **slides and spreadsheets** as outputs;
  **scheduled recurring tasks**; completion notifications to mobile; **interrupt/steer mid-task** without
  losing progress; agent may ask clarifying questions; **approval checkpoints** for real-world actions,
  watch-mode for sensitive sites, refusal of high-risk tasks (bank transfers); prompt-injection defenses;
  monthly message quotas.
- **Help center (current, legacy notice):** screenshot-driven virtual browser, connectors as data sources,
  login takeover, daily/weekly/monthly schedules, mid-task guidance/interruption, watch-mode, rate limits
  (Plus 40 / Pro 400 msgs/month), Enterprise default-off with RBAC.
- **Building agents track (engineering):** agent = instructions + guardrails + tools; primitives =
  models/tools/state/orchestration; reasoning vs non-reasoning model selection (fast chat model + strong
  model for demanding subtasks); Responses API **stateful by default** vs Agents SDK; built-in tools
  (web search, managed file-search RAG, **code interpreter** sandbox, computer-use action loop, image
  gen, MCP); **multi-agent is NOT the default** — split only for non-overlapping tasks with complex
  instructions or many tools; manager/router pattern; agent-as-tool; handoffs as first-class primitive;
  guardrails (input jailbreak filters, structured outputs, output rules); HITL accept/reject demo;
  **zero-config tracing** + per-agent focused evals; sessions primitive; errors-and-recovery docs.

---

## 2. Architectural principles (publicly documented only)

1. One conversational entry; the system selects tools and workflows dynamically (all three sources).
2. Plan → act → observe loops with explicit stopping conditions (iteration cap / confidence) (IBM ReAct).
3. Shared memory/intermediate outputs as the collaboration substrate; result aggregation by an orchestrator
   (IBM collaboration).
4. Multiple reasoning strategies behind one interface; enterprise grounding via retrieval (IBM reasoning).
5. Escalate to humans only after adaptive attempts, with a summarized report; log solutions for reuse
   (IBM workflows).
6. Interop standards: MCP for tools, A2A/ACP for agent-to-agent; opaque agents; webhook push for async
   completion (IBM A2A, Manus API).
7. Multi-agent is a deliberate choice, not a default: split for non-overlapping complex tasks; otherwise a
   single agent + tools is simpler and cheaper (OpenAI track).
8. Guardrails and HITL approvals are first-class, with tracing visible in the product (OpenAI track).
9. Statefulness/sessions and resumability are platform features, not app afterthoughts (OpenAI Responses API,
   A2A task lifecycle).

*(Zhanlu-specific architectures below are recommendations/assumptions, clearly marked.)*

---

## 3. Zhanlu current implementation (code audit, 2026-10-31)

| Area | State | Key anchors (working tree) |
|---|---|---|
| GA single entry (v2/v3), auto tool/skill/agent discovery, tool_choice forcing, turn planning, replan | **IMPLEMENTED AND VERIFIED** | `agents.py:6923/11977`, `tool_registry.resolve_tools_for_agent:681`, `delegate_tool._list_available_agents:1149`, `agents.py:6628/7661`, `turn_planner.build_turn_plan:162`, `replan_policy.py:83` |
| ReAct loop (interleaving, observations, caps, wrap-up, reflexion, context compaction) | **IMPLEMENTED AND VERIFIED** | `agents.py:7563/7618/7718/4596/7744` |
| Plan-DAG execution + parallel prepass + recovery | **IMPLEMENTED BUT NOT DEFAULT** (flag + trigger gated) | `plan_dag.generate_plan:500`, `capability_router._parallel_ready_batch_prepass:638`, `config.py:799` |
| Delegation + broker governance (cycle/dedup/depth/children/token budgets, cancellation) | **IMPLEMENTED AND VERIFIED** | `delegate_tool._delegate_task:244/_run_sub_agent:449`, `broker.evaluate_delegation:106` |
| Shared evidence registry + REST | **IMPLEMENTED AND VERIFIED** (sanitized) | `shared_evidence.py`, `executions.py:156` |
| Execution tree + live activity + evidence viz | **IMPLEMENTED AND VERIFIED** | `agent_invocations.build_agent_tree:437`, `AgentExecutionTree.jsx`, `LiveActivityStream.jsx` |
| Tool/skill registry + MCP (OAuth, reconnect, invoke) | **IMPLEMENTED AND VERIFIED** | `tool_registry.py:63/612`, `routers/mcp.py:155/273/502/579` |
| A2A / ACP (inbound) | **IMPLEMENTED AND VERIFIED** (inbound only) | `routers/a2a.py:92/104/126-197`, `routers/acp.py:62/138-190`, `services/a2a/tasks.py:194` |
| Memory & session (consolidation, project memory, CBR, procedural, context assembly) | **IMPLEMENTED AND VERIFIED** (post-mortem learning default-off) | `memory_manager.py:73/264`, `project_memory.py`, `case_based_reasoning.py:35`, `context_assembler.assemble_context:92`, `loop_postmortem.py:52` (flag off) |
| Artifacts & reports (xlsx/pptx/docx/pdf/html/md exports, previews) | **IMPLEMENTED AND VERIFIED** | `artifact_tool._create_artifact_tool:886`, `sandbox_tool.py:93-98`, `FilePreviewer.jsx` |
| Automation / long-running (execute, cancel, retry, approval resume, 12h scheduler, webhooks) | **IMPLEMENTED AND VERIFIED** (in-process scale limits) | `automation_executor.execute_automation:1019`, `scheduled_tasks.start_scheduled_tasks:741`, `workflows.py:396-454` |
| Approvals / HITL (loop pause, governance endpoints, escalation, UI handling) | **IMPLEMENTED AND VERIFIED** (thin UI) | `agents.py:8271`, `governance.py:85-148`, `Chat.jsx:2582` |
| Files/attachments + vision bridge | **IMPLEMENTED AND VERIFIED** | `Chat.handleUploadFile:3279`, `image_understanding.should_bridge:101` |
| Web search (5 providers, failover, SERP-poison filter) | **IMPLEMENTED, RUNTIME UNVERIFIED** (keyless fallbacks fragile) | `web_search_tool.py:48/75/98/147/211/345` |
| Browser / computer use | **IMPLEMENTED, DEPLOYMENT UNVERIFIED** (external Rust/headless deps) | `agent_browser_tool.py`, `computer_use/manager.py` |
| Sandbox code execution | **PARTIALLY IMPLEMENTED** (artifact-oriented Docker sandbox; not an interactive REPL) | `sandbox_tool.py:1-3`, `sandbox_worker/main.py` |
| Chat UX (department chip, steer, cancel, uploads, skill/agent/workflow pickers, deep research) | **IMPLEMENTED AND VERIFIED** (model picker MISSING — read-only badge) | `Chat.jsx:672/1289/2979/3279/3458`, `EffectiveModelBadge.jsx:10` |
| Failure recovery (SQL heal, observation recovery, fallback synthesis) | **PARTIALLY IMPLEMENTED** (no generic tool-call healing; post-mortem off) | `db/syntax_heal.py:86`, `agents.py:2591/2704` |
| Model selection per conversation | **MISSING** | `EffectiveModelBadge.jsx:10-13` |
| Celery / durable task queue | **MISSING** (in-process threads + PG advisory lock) | `scheduled_tasks.py:741-832` |
| ReAct checkpoint-resume of an interrupted run | **MISSING** (resume endpoint exists but narrow; DAG-only persistence) | `agents.py:8997`, `fsm.py:1478` |
| Outbound A2A federation (register with remote agents, outbound delegation) | **MISSING** (local executor only) | `services/a2a/tasks.py:194` |
| Rich approval workbench (diffs/previews/batch) | **MISSING** (text bubble + badge) | `Chat.jsx:2582`, `AgentExecutionTree.jsx:229` |
| Evals | **PARTIALLY IMPLEMENTED** (`eval_results` observed empty in earlier audit) | eval tooling present, no population |

---

## 4. Capability matrix

| Capability | Manus (documented) | ChatGPT agent (documented) | Zhanlu current | Gap | Priority |
|---|---|---|---|---|---|
| One conversational entry, system picks workflow | Yes (positioning) | Yes (unified agent) | **Yes — GA + discovery** | — | — |
| Autonomous multi-step task execution | Yes (API tasks) | Yes (virtual computer) | **Yes — ReAct + delegation** (verified) | — | — |
| Multi-turn task continuation | Yes (API multi-turn) | Yes (steer/resume mid-task) | Partial — steer/cancel/resume endpoints; **no ReAct checkpoint-resume** | Resumable runs | **P1** |
| Project-level shared instructions | Yes (projects) | — (not documented) | **Yes — project memory + department metadata** (verified) | — | — |
| File attachments | Yes (PDF/img/CSV) | Yes | **Yes** (verified) | — | — |
| Generated deliverable files | Yes (slides/sites/etc.) | Yes (slides/sheets, legacy) | **Yes — xlsx/pptx/docx/pdf/html/md** (verified) | none material | P3 polish |
| Deliverable preview in chat | Yes (products) | Yes | **Yes — FilePreviewer + 3D viewer** | — | — |
| Web search | implied (Wide Research) | Yes (browser) | Yes — 5 providers + failover (runtime unverified) | harden keyless fallback | **P2** |
| Browser interaction / computer use | implied (browser companion) | Yes (visual+text browser, takeover) | Present (browser/computer-use tools) — **deployment unverified** | verify in runtime | **P2** |
| Code execution | implied (games/sites) | **Yes — terminal / code interpreter** | **Partial — Docker artifact sandbox, no interactive REPL** | general code-exec loop | **P1** |
| Long-running / background tasks | Yes (async + webhooks) | Yes (mobile completion notifications) | Partial — automations in-process; **no queue, no run-completion webhook** | queue + webhooks | **P1** |
| Scheduling / recurring tasks | Not documented (homepage) | **Yes — daily/weekly/monthly schedules** | **Yes — scheduled_tasks + automation cadence** (verified) | UX parity check | P3 |
| Completion webhooks/notifications | **Yes (API v2)** | Yes (mobile push) | Partial (workflow webhooks only) | run-completion webhooks | **P2** |
| Skills / custom agents | **Yes (API v2)** | Yes (connectors/GPTs ecosystem) | **Yes — skills registry + AgentApp publish lifecycle** (verified) | — | — |
| Agent-to-agent protocol | Not documented | Not documented | **Yes — A2A + ACP inbound** (verified) | outbound federation | P2 |
| MCP tool interop | Not documented | Yes (built-in MCP) | **Yes — full MCP stack** (verified) | — | — |
| HITL approval checkpoints | Not documented | **Yes (confirmations, watch-mode)** | **Yes — plan gate + governance approvals** (verified) | rich approval UI | **P2** |
| Mid-task steering / interruption | implied (multi-turn) | **Yes (documented)** | **Yes — steer + cancel** (verified) | — | — |
| Model selection per conversation | "All models" page | implicit (model picker) | **MISSING** (read-only badge) | in-chat selector | **P1** |
| Shared memory across agents | Not documented | agent-as-tool + conversation-keyed memory (engineering doc) | **Yes — shared evidence + context bus + CBR** (verified) | — | — |
| Execution transparency / tracing | Yes (task replay implied by product) | Yes (narration of actions) | **Yes — LiveActivityStream + execution tree + invocations** (verified) | — | — |
| Evals | Not documented | Yes (focused per-agent evals) | **Partial — tooling exists, not populated** | wire eval runs | P2 |
| Result aggregation by orchestrator | implied | Yes | **Yes — GA synthesis + reviewer** (verified) | — | — |
| Failure recovery / self-healing | Not documented | Yes (continue after interruption) | **Partial — SQL heal + observation recovery; no generic tool-call heal** | tool-call healing | P2 |
| Enterprise governance (RLS, risk-tiered actions, ontology, audit) | Not documented (SSO/teams only) | Enterprise RBAC toggle | **Beyond benchmark — ontology, RLS, risk tiers, audit, evidence chain** (verified) | — | — |

Legend: "verified" = code-inspected this session; Zhanlu entries marked (verified) also appear in the
regression runs below. Benchmark cells cite only publicly documented behavior.

## 5. Already-exists-but-needs-improvement

1. **Web research chain** — keyless Bing/DDG HTML fallbacks are brittle; add provider health checks +
   cached-credential rotation; verify browser/computer-use deployment end-to-end.
2. **Sandbox** — evolve the artifact-oriented Docker sandbox toward a session-scoped interactive execution
   loop (persist a workspace across steps, accept iterative stdio, keep the existing isolation).
3. **Automation runtime** — introduce a durable queue (Celery/Redis or PG-based) behind the existing
   `automation_executor` interface; add run-completion webhooks (pattern already exists in `workflows.py`).
4. **Approvals UX** — replace the text-bubble flow with an approval workbench (show payload diffs,
   evidence links, approve/reject with comment; reuse governance endpoints unchanged).
5. **Post-mortem learning** — `LOOP_POSTMORTEM_ENABLED` default-off; enable per-project with a sampling
   rate and pipe outcomes into the existing procedural-promotion pipeline.
6. **Evals** — populate `eval_results` from replayed golden scenarios (P1 scenario harness exists).

## 6. Recommended implementation (priority order)

| # | Recommendation | Reuses | Priority |
|---|---|---|---|
| R1 | In-chat model selector (conversation-scoped model override; GA keeps discovery/steering) | existing `llm_router` hierarchy + `EffectiveModelBadge` slot | **P1** |
| R2 | Run-completion webhooks for chat-initiated runs + automations | `workflows.py` webhook pattern; A2A task lifecycle states | **P1** |
| R3 | Interactive code-execution step inside ReAct (sandbox session with persistent workspace + iterative outputs rendered like data_offer) | `sandbox_tool` images + `sandbox_worker`; `data_offer` preview path | **P1** |
| R4 | Durable queue for automations/scheduled work (multi-worker safe; keep in-process mode as fallback) | `automation_executor` + PG advisory-lock pattern | **P1** |
| R5 | ReAct checkpoint-resume (persist per-iteration state; resume endpoint resumes mid-loop) | existing `_resume_state` + resume endpoint | **P2** |
| R6 | Rich approval workbench UI | governance REST (unchanged) | **P2** |
| R7 | Outbound A2A: Agent-Card fetch + remote task submission reusing the inbound task model | `routers/a2a.py`, `models/a2a_task.py` | **P2** |
| R8 | Web-research hardening + runtime verification of browser/computer-use | `web_search_tool` failover chain | **P2** |
| R9 | Enable post-mortem learning with sampling; wire eval population | `loop_postmortem`, P1 scenario harness | **P3** |

## 7. Dependencies and risks

- **R2/R4** need Redis or PG queue infrastructure in the deployment (currently none for tasks; Redis exists
  optionally for governance/evidence stores). Risk: new operational surface; mitigation — feature-flagged,
  in-process fallback retained.
- **R3** requires the prebuilt sandbox images on every host (already a deployment constraint) and a
  wall-clock/memory cap per session (reuse SBX-1 isolation policy).
- **R1** touches prompt/model-routing paths used by every turn; must honor per-agent model overrides and
  cost metering already in `llm_router`.
- **R5** risks stale-state resume bugs; mitigate by resuming only from persisted typed events + invocation
  records (already authoritative for the tree UI).
- **R7** outbound federation multiplies the trust boundary — agent-card auth schemes must be validated
  before any cross-org delegation; keep disabled by default.
- Cross-cutting: the provider-quota blocker (429 insufficient_quota) still gates all live LLM acceptance;
  none of the above can be live-verified until funded.

## 8. Priority summary

**P1:** R1 model selector · R2 completion webhooks · R3 interactive code execution · R4 durable queue.
**P2:** R5 checkpoint-resume · R6 approval workbench · R7 outbound A2A · R8 web-research hardening.
**P3:** R9 post-mortem/evals · deliverable-preview polish.

**Where Zhanlu is already ahead of the public benchmarks:** governed enterprise data access
(NL2SQL + RLS + source-verification gates), ontology + risk-tiered actions, governed multi-agent
collaboration (broker, contracts, cycle/dedup/depth/child/token budgets), shared evidence with a sanitized
REST surface, execution-tree transparency, and inbound A2A/ACP — none of which the reference products
document publicly.

---

## Appendix — this session's regression evidence (fresh runs, 2026-10-31)

Sanitizer/hardening 26 · live_events 53 · sse_builders 13 · live_plan_persistence 3 · turn_planner 43 ·
p3_execution_tree 10 · agent_invocations 5 · invocation_enrichment 5 · p1_orchestration 13 ·
p1_scenarios 5 · p1p2_hybrid 12 · p2_production_hardening 7 (+2 skip) · fsm_tenant_isolation 15 ·
db_tools_validation_hints 8 · query_contract 29 · nl2sql_gaps 11 · nl2sql_db_error_repair 23 — all passed ·
frontend chat suites 10 passed · production build exit 0.
