# Zhanlu — AI Agent Technical Reference

> **Document purpose.** A long-term, detailed technical reference for the Zhanlu
> agent platform: architecture, components, request lifecycle, workflows,
> decision logic, tools and integrations, configuration, dependencies, and
> hard constraints. Organized to mirror the IBM AI-agents framework
> (Introduction → Components → Architecture → Protocols → Lifecycle & operations
> → Multi-agent systems → Frameworks → Governance → Agentic RAG → Use cases)
> so it can be maintained section-by-section as the system evolves.
>
> **Last updated:** 2026-10-09 · **Backend:** FastAPI (Python 3.11) ·
> **Entry point:** `backend/main.py` · **Core agent loop:** `backend/app/routers/agents.py`

---

## Table of Contents

1. [What the Agent Is](#1-what-the-agent-is)
2. [System Architecture](#2-system-architecture)
3. [Core Components (IBM Components Framework)](#3-core-components)
4. [Request Lifecycle: Input → Output](#4-request-lifecycle)
5. [Main Workflows & Decision Logic](#5-main-workflows--decision-logic)
6. [Tool Calling & the Tool Registry](#6-tool-calling--the-tool-registry)
7. [Inter-Agent Protocols (MCP / A2A / ACP)](#7-inter-agent-protocols)
8. [Multi-Agent Systems](#8-multi-agent-systems)
9. [NL2SQL & the Data Agent](#9-nl2sql--the-data-agent)
10. [Agentic RAG](#10-agentic-rag)
11. [Memory System](#11-memory-system)
12. [Learning & Adaptation](#12-learning--adaptation)
13. [Governance, Security & Human-in-the-Loop](#13-governance-security--human-in-the-loop)
14. [Observability & Operations](#14-observability--operations)
15. [Configuration & Setup](#15-configuration--setup)
16. [Key Dependencies](#16-key-dependencies)
17. [Technical Constraints & Known Limitations](#17-technical-constraints--known-limitations)
18. [How to Extend the System](#18-how-to-extend-the-system)
19. [Appendix A: Module Map](#appendix-a-module-map)
20. [Appendix B: Test Suite Map](#appendix-b-test-suite-map)

---

## 1. What the Agent Is

Zhanlu (湛卢) is a **multi-tenant, enterprise AI agent platform**. The primary
artifact is a governed **tool-calling agent runtime** exposed through chat
(SSE streaming) and APIs, plus a platform layer for building, versioning,
deploying, and governing many agents at once.

Following the IBM framing: Zhanlu is not a single model — it is a system in
which an LLM **autonomously performs tasks by designing workflows with
available tools**. The platform implements all seven IBM agent components
(perception, planning, reasoning, memory, tool calling, communication,
learning) as explicit, testable subsystems.

**Positioning vs. frameworks (IBM "Frameworks" section):** Zhanlu is a
custom framework in the same problem space as LangChain/LangGraph/AutoGen,
distinguished by (a) a governance-first design (approval gates, risk tiers,
RBAC, audit), (b) a production NL2SQL subsystem with a semantic store, and
(c) sandboxed execution. It also integrates with those frameworks where
useful (LangGraph appears in the separate EDIA project; Zhanlu itself does
not depend on it).

**Core platforms delivered:**
- **Zhanlu** — enterprise multi-agent platform (this document)
- **EDIA** — petrochemical decision-intelligence system built on the same team's LangGraph patterns (separate codebase; not covered here)

---

## 2. System Architecture

### 2.1 Runtime topology

```
┌────────────┐   SSE    ┌─────────────────────────┐
│  React UI  │◄────────►│  FastAPI backend (:5002)│
│ (:8089)    │  REST    │  app/routers/agents.py  │
└────────────┘          │  • v3 stream (ReAct)    │
                        │  • v2 stream (FSM opt.) │
┌────────────┐          └───────┬─────────────────┘
│ PostgreSQL │◄─────────────────┤
│ (state)    │                  │ Redis (queues/cache/locks)
├────────────┤                  │ MinIO (objects)
│ MySQL/MSSQL│◄── data agent ───┤
│ Oracle (…) │                  │
└────────────┘          ┌───────▼─────────────────┐
                        │ sandbox-worker (Docker) │  Redis-queued
                        │ no network, ro-fs       │  isolated code exec
                        └─────────────────────────┘
```

- **Single FastAPI process** hosts the agent loop, tool registry, NL2SQL
  pipeline, scheduler loops, and protocol endpoints. Multi-worker scaling is
  possible for stateless parts, but see [§17](#17-technical-constraints--known-limitations)
  for in-process state (cancel flags, rate-limit windows, metrics).
- **Sandbox worker** is a separate container that exclusively holds the
  Docker socket and executes untrusted code in locked-down containers.

### 2.2 Repository layout (backend)

```
backend/
├── main.py                     # app assembly, lifespan, auto-migrations
├── app/
│   ├── routers/                # agents.py (the agent loop), nl2sql.py,
│   │                           # a2a.py, acp.py, external_agents.py,
│   │                           # agent_lifecycle.py, project_catalog.py,
│   │                           # terminologies.py, admin_observability_traces.py …
│   ├── services/
│   │   ├── agent_prompts.py    # system prompts + get_tools() assembly
│   │   ├── agent_tools.py      # execute_tool / execute_tool_with_retry
│   │   ├── tool_registry.py    # registry singleton, RBAC, risk metadata
│   │   ├── tool_handlers/      # 65 handler modules (web, db, sandbox, …)
│   │   ├── db/                 # NL2SQL subsystem (39 modules, see §9)
│   │   ├── memory_advanced/    # semantic recall, embeddings, scorer
│   │   ├── swarm/              # multi-agent runtime, mailbox, group chat
│   │   ├── governance/         # approval service, agent-tier policy
│   │   ├── a2a/ acp/ mcp_client/ mcp_server/
│   │   ├── rl/                 # rewards, policy bandit, SFT export, ART
│   │   ├── tracing/            # OpenTelemetry + in-process metrics
│   │   ├── scheduled_tasks.py  # periodic loops (post-mortem, monitors…)
│   │   └── knowledge_graph/    # workload miner, domain discovery
│   └── models/                 # SQLAlchemy models (~60 tables)
├── tests/                      # pytest suites (hundreds of files)
└── scripts/                    # seeding, golden cases, ops helpers
```

---

## 3. Core Components

Mapped one-to-one to the IBM "Components of AI agents" framework.

### 3.1 Perception and input handling

- **Input modalities:** text (EN/ZH), file uploads (images forwarded as
  multimodal content blocks with OCR text, documents routed to RAG
  ingestion), and `file_urls` persisted across turns.
- **Preprocessing:** message sanitization, caller-hint sanitization
  (`<caller-hint untrusted>` wrapping), untrusted-content neutralization
  gate, per-request trace context (user, role, agent) for RBAC.
- **Data freshness perception:** catalog windows (min/max dates per table)
  and a "data freshness" question the answer contract must disclose.

### 3.2 Planning and task decomposition

- **Turn planner (deterministic):** per-turn plan with steps ticked off by
  tool evidence (`TURN_PLAN_ENABLED`), emitted as `plan_step_added` SSE.
- **LLM planner (NL2SQL):** `services/db/nl2sql_planner.py` — one bounded
  LLM call resolving entity/metric/time/slice from the semantic store's
  VERIFIED vocabulary; structured clarification when uncertain;
  `Nl2sqlTurnState` persists resolved entities, accepted defaults and
  rejected interpretations per (conversation, KB).
- **Plan DAG (FSM route):** `services/synexia/plan_dag.py` — upfront DAG of
  tool/skill/sandbox/nl2sql nodes with a user approval gate and bounded
  replanning (`SYNEXIA_*` flags; FSM route optional).
- **ReWOO mode:** `services/rewoo.py` — plan→execute→solve (2 LLM calls)
  for research-shaped sub-agent tasks; fails open to ReAct
  (`REWOO_ENABLED`, default off).

### 3.3 Reasoning and decision-making

- **Production loop:** interleaved ReAct (think–act–observe) in
  `routers/agents.py` with:
  - iteration budgets (`MAX_TOOL_ITERATIONS`, per-conversation
    `IterationBudget`, per-agent overrides),
  - wall-clock cap,
  - tool-call loop guard (`_detect_tool_call_loop`, per-tool caps in
    `TOOL_CALL_CAPS`, turn-scoped, success-aware, one "tool-free
    synthesis pass" that is genuinely tool-free),
  - signal-based loop-end nudges after grounding-tool success,
  - wrap-up nudges near the budget end,
  - post-loop synthesis with grounding/quality gates.
- **Verification:** deterministic validators + optional LLM-rubric VERIFY
  stage (FSM), grounding/quality gates post-loop, apology/bounce-back
  guards replacing non-answers.
- **Reasoning support:** think tool (explicit Thought step), chain-of-thought
  protocols in synthesis prompts, Reflexion-style re-synthesis
  (`SYNTHESIS_REFLEXION_ENABLED`).

### 3.4 Memory

CoALA-aligned, three tiers plus working context:

| Tier | Storage | Notes |
|---|---|---|
| Short-term | conversation message history + 3-layer compaction (`services/compaction/`) | microcompact / session memory / full compact; pre-API pruning; tool-output caps |
| Episodic | `agent_memories` with `memory_type='episodic'` | task→solution cases recorded after successful turns (`case_based_reasoning.py`); recalled for similar future tasks |
| Semantic | `agent_memories` (`target='memory'/'user'`, default) | agent notes + user profile; embeddings stored in-table; project-scoped |
| Procedural | `agent_memories` (`memory_type='procedural'`) | playbooks distilled from repeated successful cases (`procedural_promotion.py`, scheduled) |
| Working scratchpad | in-loop message list | tool results spill to disk over size thresholds (`tool_result_persistence.py`) with read-back pointers |

Semantic recall: `memory_advanced/` (embeddings with Redis/LRU cache,
cosine + recency + importance scoring). Embeddings: OpenAI-compatible API
or **local fallback** (`sentence-transformers/all-MiniLM-L6-v2`, flag
`EMBEDDING_LOCAL_FALLBACK_ENABLED`).

### 3.5 Action and tool calling

See [§6](#6-tool-calling--the-tool-registry). Summary: a registry of ~65
handler modules plus CRUD tools, OpenAI-function-call schemas, RBAC
filtering, risk metadata, approval gates, self-healing retries, and a
`search_tools` meta-tool for discovery under selective schema injection.

### 3.6 Communication

- **Human-facing:** SSE streaming (deltas, activity steps, plan events,
  artifacts, done) + REST; steer (`/steer`) and cancel (`/cancel`) endpoints
  for mid-turn interaction.
- **Agent-facing:** MCP (client+server), A2A (client+server), ACP adapter,
  external-agent registry — see [§7](#7-inter-agent-protocols).
- **Internal:** ContextBus (`services/context_bus.py`) shares artifacts,
  transcript and memory across agents; swarm mailbox for async messaging.

### 3.7 Learning and adaptation

See [§12](#12-learning--adaptation). Summary: trajectory rewards persisted
per invocation → ε-greedy policy bandit over routes → HITL thumbs override
rewards → SFT dataset export → ART/GRPO training wiring; routing feedback
adjusts the FSM-vs-ReAct threshold; scheduled post-mortems mine failed
loops into lessons.

---

## 4. Request Lifecycle: Input → Output

The v3 streaming path (`POST /api/apps/{app}/agents/conversations/{c}/messages/stream`)
is the production entry point. Simplified but faithful sequence:

```
1. Auth + trace context          user JWT; TraceContext(session,user,agent,role)
2. Conversation & state          load conversation; auto-rebind templates;
                                 link ChatSession; restore deep links
3. Prompt assembly               agent definition / 5-layer AgentApp prompt
                                 + tone + initiative + date anchor
                                 + grounding block (if time-sensitive)
                                 + skills, MCP tools, data-source binding
                                 + memory / case-based reasoning block
4. Tool assembly                 get_tools() → RBAC-filtered registry schemas
                                 + CRUD schemas + deck/MCP tools
                                 → semantic tool-selection (top-K + search_tools)
                                 → grounding pinning (web_search front)
5. Planner (optional)            nl2sql_planner.plan_question(): entity/metric/
                                 time/slice; clarification payload when
                                 ambiguous; turn-state carried forward
6. Data-source runtime           bound KBs, dialect, capability probe,
                                 readiness gate
7. Agent loop (ReAct)            per iteration:
                                   a. guard scan (loop detection, budgets,
                                      wall clock, user cancellation)
                                   b. finish-line / wrap-up nudges
                                   c. LLM call (streamed deltas + reasoning)
                                   d. tool execution (parallel batches,
                                      approval gates, retries, spill-to-disk)
                                   e. activity steps + progress SSE
                                   f. post-answer gates (grounding, apology,
                                      bounce-back, deliverable phase lock)
8. Post-loop                     synthesis / artifact delivery / fallback
                                 chain (recovered content → honest reason →
                                 context-aware fallback), hygiene pass
9. Persistence                   assistant message, invocations + trajectory
                                 rewards, run steps, experience entries
10. Terminal event               done SSE (content, trace, conversation,
                                 status; "cancelled" if user halted)
11. Fire-and-forget              memory auto-extraction, episodic case
                                 recording, SFT promotion, cache eviction
```

Key SSE event types: `delta`, `tool_progress`, `activity_step`,
`plan_step_added`, `phase`, `artifact_created`, `live_event`, `paused`,
`llm_retry`, `done`.

---

## 5. Main Workflows & Decision Logic

### 5.1 Tool-selection decision logic

Eligible tools = AgentApp config (`enabled_tools`) ∪ agent definitions
whitelists ∪ MCP grants ∪ CRUD defaults, then RBAC filtering. When the
eligible catalog exceeds `TOOL_SEARCH_TOP_K` (32), `select_relevant_tools()`
injects only the top-K semantically ranked schemas plus an essential set
and the `search_tools` meta-tool (the model can discover and then call
filtered-out tools — execution resolves by name from the registry).

### 5.2 Loop-guard decision logic (`_detect_tool_call_loop`)

- Turn-scoped scan; key = `(tool, canonicalized args)` — different args =
  investigation (allowed), same args repeated = loop.
- Success-aware: successful calls trip at `cap`; failed calls get one extra
  reformulation chance (`cap + 1`); in-progress calls count as success.
- Per-tool caps: `memory: 3`, `ask_data_agent: 6 (dynamic)`,
  `fetch_data_batch: 3 (dynamic)`, `interrupt: 2`, `clarify: 3`; default =
  `TOOL_CALL_HARD_CAP` from the harness policy.
- First trip → one genuinely tool-free synthesis pass (`tools=[]`,
  `tool_choice="none"`). Second trip → break; the post-loop path then
  prefers recovered content and, if the guard fired, an honest user-facing
  reason (never the internal nudge text).

### 5.3 Data-question routing

When the question is data-shaped and a KB is bound, the loop can force
`ask_data_agent` (delegated data agent) which internally runs the NL2SQL
pipeline (§9) with its own 4-iteration budget, synthesis, and grounding
gate — reports with zero data-source calls fail (anti-fabrication).

### 5.4 Deliverable workflows

PPTX/DOCX/XLSX/PDF/HTML generation flows through plan gates that force the
build tool, phase-lock the deliverable, hold artifacts behind a quality
gate (confidence threshold), and stream inline previews. Dashboard builds
have a dedicated forcing/rescue mechanism and post-build verification.

### 5.5 Failure and recovery

LLM errors → structured classification (`classify_api_error`) → bounded
retries with backoff → provider failover (candidate model) → graceful SSE
`done` so the frontend always unblocks. Empty answers → recovered-best →
honest guard-trip reason → context-aware fallbacks (artifact-aware,
data-summary, dashboard redirect) → generic last resort. A scheduled
**turn-health monitor** flags fallback/apology turns (CRITICAL when ≥15%
over ≥5 turns in 24h).

---

## 6. Tool Calling & the Tool Registry

### 6.1 Registry

`services/tool_registry.py` — singleton; entries carry OpenAI schema,
handler, category/toolset, `enabled_by_default`, `requires_config` /
`requires_env`, `check_fn` availability probe, `risk`
(`read|write|destructive|external`), `dangerous_actions`, result-size caps.

- **Handlers:** ~65 modules in `app/services/tool_handlers/` (web search /
  extract, sandbox code execution, database, delegation, memory, todo,
  image generation, file ops, kanban, cron, approvals, admin env config,
  process registry, fuzzy match, URL safety, vision, transcription, …).
- **CRUD tools** live separately (`agent_tools._CRUD_DISPATCH`) and are
  merged into the schema list.

### 6.2 Execution path

`agent_tools.execute_tool_with_retry()`:
1. RBAC + permission check (`check_permission`) — deny, or
   `requires_confirmation`.
2. Agent-tier policy (`governance/agent_tier.py`): high/critical risk-tier
   agents force confirmation on write/destructive tools; per-agent
   `rate_limit_per_minute` enforced by a sliding window.
3. Plan-mode: non-execution calls become proposals.
4. System meta-agents (agent_builder, skill_agent, automation_agent,
   general_assistant, power_user) bypass the confirmation gate (user
   consent = starting the conversation).
5. Approval gate: an `ApprovalRequest` (risk high/medium) pauses execution
   until the user approves via the governance API; **fail-closed** on
   approval-service errors.
6. Execution with result-level retry + one-shot LLM argument
   reformulation (self-healing), layered over per-handler reliability.
7. Result post-processing: size caps, spill-to-disk persistence, activity
   previews.

### 6.3 Selective injection & discovery

`services/tool_search.py`: ranks schemas against the user message
(batch embeddings; lexical fallback) and injects top-K + essentials +
`search_tools` meta-tool. Execution resolves handlers by name, so
discovered tools are immediately callable in the next iteration.

---

## 7. Inter-Agent Protocols

| Protocol | Role | Implementation |
|---|---|---|
| **MCP** | agent ↔ tools (vertical) | `mcp_client/` (stdio, streamable-HTTP, OAuth), `mcp_server/` (exposes the registry as MCP tools); per-agent MCP grants |
| **A2A** | agent ↔ agent (horizontal) | `services/a2a/client.py` (JSON-RPC 2.0 `tasks/send|get|cancel`, SSE streaming via `send_task_streaming`), `routers/a2a.py` (`/.well-known/agent.json` card, X-API-Key or JWT auth), `tool_handlers/a2a_tool.py` (`invoke_external_agent`, `list_external_agents`) |
| **ACP** | REST alternative | `routers/acp.py` — `/acp/agents`, `/acp/agents/{id}/runs` (?wait=true), run messages/cancel; built on the A2A task store (shared executor) |
| **External registry** | governance of remote agents | `models/external_agent.py` + `routers/external_agents.py` (CRUD + card probe); `invoke_external_agent` resolves by id/name |

Outbound calls identify the platform: `X-Agent-Id` +
`User-Agent: ZhanluAgent/1.0 (A2A_SELF_AGENT_ID)` on A2A and MCP HTTP
connections.

---

## 8. Multi-Agent Systems

- **Main/sub-agent delegation:** `delegate_tool.py` (`delegate_task` tool,
  `_run_sub_agent_inner`) — restricted toolset, parent-context digest
  inheritance (`DELEGATE_CONTEXT_INHERITANCE`), bounded fan-out
  (`DELEGATE_MAX_CONCURRENT`), ReWOO option for research tasks.
- **Plan-DAG parallel execution:** ready-batch prepass executes independent
  plan nodes concurrently, each in its own DB session, bounded by
  `PLAN_DAG_MAX_CONCURRENT=4` (adaptive replanning forces serial).
- **Swarm runtime** (`services/swarm/`): orchestrator, worker pool, runtime,
  mailbox (async messaging), team registry. The data agent can run as a
  swarm pipeline (`DATA_AGENT_SWARM_ENABLED`, default off) with
  delegation-mode telemetry (A/B in `agent_metrics`).
- **Group chat:** `swarm/group_chat.py` — round-robin specialists, COMPLETE
  marker protocol, per-speaker credit (completer 2.0 / success 1.0 /
  failure −1.0), recorded as trajectories for learning.
- **Agent directory:** `list_available_agents` exposes builtin definitions
  + published AgentApps so the planner can choose delegates.

---

## 9. NL2SQL & the Data Agent

The data subsystem adapts Oracle's "enterprise NL2SQL with semantic
enrichments" architecture: an online execution path plus an offline
enrichment path sharing one **semantic store**.

### 9.1 Semantic store (`services/db/semantic_store.py`)

Read-only facade over four inputs, with provenance tiers and strict
per-KB scoping:

| Input | Source tables |
|---|---|
| Schema info | `kb_table_meta`, `kb_column_meta`, `kb_table_relation` (catalog indexer) |
| Safe sample data | `kb_table_meta.sample_values`, `kb_column_meta.enum_values/unit`, entity hints |
| Query workload | `nl2sql_query_logs`, `query_plan_memory`, `query_workload_stats` |
| Domain knowledge | `terminologies`, `metric_definitions`, `project_metric` (approved only), catalog overlay |

Provenance tiers: `VERIFIED` (human-curated) / `DISCOVERED` (indexed from
the datasource) / `LEARNED_FROM_WORKLOAD` (mined from executions) /
`RUNTIME_INFERRED` (per-question, never persisted, never promoted).
Runtime inference is **never auto-promoted to truth**, and workload signals
never grant authorization.

### 9.2 Online pipeline (per data question)

```
planner (intent/metric/time/slice; clarify when uncertain)
  → table retrieval (lexical + semantic + verified plans + domain scoping
    + workload pair-boost + coverage expansion)
  → join-partner guarantee (pull missing relation partners into the slice)
  → schema linking (M-Schema slice, access-policy filtered)
  → grounded plan (unsupported slots named to the model BEFORE SQL)
  → dialect-specific SQL generation (Oracle/Postgres/MySQL/MSSQL/SQLite)
  → SQL rewriter (predicate pushdown, row limits)
  → execution gate (read-only check, table allow-lists, row-level filter
    injection, EXPLAIN cost assessment (flag), join reachability)
  → execution + self-repair loop (unknown-column heal, error-driven retry)
  → answer contracts (ratio/date grounding), report cards, chart suggestion
  → narration (LLM narrates; deterministic code computes)
```

### 9.3 Offline enrichment

- **Catalog indexer** ingests schemas into the catalog tables.
- **Workload miner** (`knowledge_graph/workload_miner.py`) parses
  successful query logs into `query_workload_stats`: `popular_table`,
  `table_pair` (co-joined), `table_filter` (column = literal). The
  SemanticStore surfaces these as popularity re-ranking, join hints, and
  common-filter prompt hints (`[WORKLOAD HINTS]` block).
- **Example promotion** (`q_sql_examples/`): contract-passing Q→SQL runs
  become few-shot examples (deduped by SQL hash, capped per datasource);
  authored examples are never overwritten.
- **Terminology authoring API** (`routers/terminologies.py`): analysts add
  VERIFIED glossary entries per datasource; consumed by the store,
  retrieval evidence, and the prompt context builder.

### 9.4 Safety properties

- Read-only enforcement (sqlglot single-SELECT + read-only check).
- Table allow-lists + row-level filter injection from access policy.
- Bounded execution (row caps, time budget) and EXPLAIN-based scan-size
  assessment (flag `NL2SQL_EXPLAIN_FEEDBACK_ENABLED`, off by default).
- Ungrounded reports (zero datasource calls) fail instead of fabricating.
- Data-source credentials never reach the sandbox.

---

## 10. Agentic RAG

`document_ingestion/` + `services/db/` implement the IBM agentic-RAG
patterns:

- **Ingestion:** document parsing → chunking (fixed, semantic, or
  **agentic chunking** where an LLM groups consecutive blocks into coherent
  units — flag-gated) → embeddings (bge-m3 locally or API) → ChromaDB.
- **Retrieval:** hybrid lexical + vector; **retrieval grading** (LLM judges
  whether passages suffice → **corrective retry**); **query decomposition**
  for multi-hop questions; **semantic cache** for repeated queries.
- **Binding:** evidence bound to business questions by product/time/source
  metadata; citations extracted into answers.
- Routing: large uploads are auto-routed to RAG; small ones inline.

---

## 11. Memory System

Detailed layout in [§3.4](#34-memory). Operationally notable:

- **Recording:** `agent_invocations.record_invocation()` persists every
  agent turn (tokens, cost, duration, tools, route, trace id) and computes
  a trajectory reward; `apply_human_feedback()` lets thumbs up/down write
  `source="human"` reward overrides.
- **Extraction:** post-conversation LLM auto-extraction of memories
  (project-scoped), plus episodic case recording and SFT promotion hooks.
- **Compaction:** 3-layer strategy keeps long tasks inside context budget;
  pruning is deterministic and pre-API.
- **Turn state:** `Nl2sqlTurnState` carries resolved entities/defaults
  across data-agent turns (OCI P1-6).

---

## 12. Learning & Adaptation

| Mechanism | File | Flag (default) |
|---|---|---|
| Trajectory reward function (outcome + latency + tool efficiency + quality) | `services/rl/rewards.py` | always on via `record_invocation` |
| Policy bandit (ε-greedy over route arms) | `services/rl/policy_bandit.py` | `RL_BANDIT_ENABLED=True` |
| HITL reward override (thumbs → reward → bandit) | `agent_invocations.apply_human_feedback` | always on |
| SFT dataset export (verified trajectories → JSONL) | `services/rl/art_export.py` | CLI/admin |
| ART/GRPO training wiring (OpenPipe ART, LoRA on Qwen via vLLM) | `services/rl/art_training.py` | `RL_ART_*` env; GPU server |
| Routing feedback (FSM-vs-ReAct threshold from measured success/latency; bandit signal fallback) | `services/routing_feedback.py` | `ROUTING_FEEDBACK_ENABLED` (off) |
| Loop post-mortem (failed loops → lessons memory) | `services/loop_postmortem.py` | `LOOP_POSTMORTEM_ENABLED` (off) |
| Procedural promotion (repeated cases → playbooks) | `services/procedural_promotion.py` | `PROCEDURAL_PROMOTION_ENABLED=True` |
| Example auto-promotion (successful Q→SQL → few-shot corpus) | `q_sql_examples/service.py` | `NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED=True` |

Principles: advisory-only (callers keep deterministic fallbacks), rewards
never fatal, learning writes are deduped and capped, RUNTIME_INFERRED
never promoted.

---

## 13. Governance, Security & Human-in-the-Loop

### 13.1 Approval gates & HITL

- Deterministic approval requests for write/destructive tools (risk from
  registry metadata, not model judgment); paused turns persist resume
  state; fail-closed on errors.
- Agent risk tier (`AgentApp.policy_profile`) escalates gating for
  high/critical agents and enforces per-agent tool rate limits.
- Thumbs feedback → `TrajectoryReward(source="human")` → bandit.

### 13.2 Access control

- JWT auth; RBAC tool filtering inside `get_schemas` via `TraceContext`
  role; access-policy service strips denied tables/columns from schema
  context and injects row-level filters; semantic store is structurally
  scoped to authorized KB ids.

### 13.3 Sandbox

Redis-queued Docker worker exclusively holds the Docker socket; job
containers run with no network, read-only filesystem, dropped
capabilities; LLM access through a Unix-socket proxy; secrets and
datasource credentials never enter execution containers.

### 13.4 Agent lifecycle (ADLC)

- Versioned harnesses: `AgentApp.version` + `rollout_percent` +
  immutable `AgentAppRevision` snapshots; publish/unpublish/rollback
  (`services/agent_lifecycle.py`).
- Canary rollout: deterministic md5 bucket gate on A2A `tasks/send` and
  ACP runs.
- Red-team campaigns: 6 adversarial probes with deterministic
  must-not-include/refusal criteria, scheduled every 6h
  (`RED_TEAM_ENABLED`, default off).
- Governed catalog: `GET /api/agents-catalog` — version, owner, risk tier,
  rollout, last red-team result.

### 13.5 Interruption & steering

- `POST …/conversations/{id}/steer` — inject a mid-turn message drained
  between iterations.
- `POST …/conversations/{id}/cancel` — graceful mid-turn cancellation
  (`services/run_control.py`); loop stops at the next iteration
  checkpoint, partial content is preserved, `done` carries
  `status:"cancelled"`. The frontend Stop button calls both.

---

## 14. Observability & Operations

- **Tracing:** OpenTelemetry SDK + OTLP exporter; trace/span ids propagated
  into invocations; `GET /admin/traces/metrics` in-process GenAI metrics.
- **Run spine:** `AgentRun` + `AgentRunStep` per turn/model call/tool call
  (`AGENT_HARNESS_ENABLED`), queryable via `/api/agent-runs/...`.
- **Invocations:** every turn/delegation recorded with tokens, cost,
  duration, tools, route, delegation mode — the substrate for rewards,
  routing feedback, and the catalog.
- **Turn-health monitor:** scheduled scan for fallback/apology answers
  (CRITICAL at ≥15% of ≥5 turns in 24h) — the early-warning system for
  answer-quality regressions.
- **Scheduled loops** (`services/scheduled_tasks.py`): daily eval,
  red-team (6h), loop post-mortem (12h), procedural promotion (24h),
  turn-health monitor (6h), automation dispatcher, catalog watchdog.
- **Logging:** structured logs with per-component loggers; agent-loop
  decision points (guard trips, hint injection, planner calls) are all
  logged.

---

## 15. Configuration & Setup

### 15.1 Runtime stack

```bash
# current dev stack
docker compose up -d          # zhanlu-backend (:5002), zhanlu-frontend (:8089),
                              # postgres, redis, minio, sandbox-worker
# backend tests (targeted; full-suite may OOM the container)
docker exec zhanlu-backend python -m pytest tests/<file> -p no:cacheprovider -q
```

- Config: `backend/app/config.py` (`Settings`, 1500+ lines, env-driven).
- Runtime auto-migrations: `_specific_migrations()` in `main.py` adds new
  columns on boot (SQLite + Postgres) — new columns must be registered
  there **and** on the model.
- Model registration: every new model must be imported in
  `app/models/__init__.py` or `create_all` will not create its table.

### 15.2 Important flags (selection)

| Flag | Default | Purpose |
|---|---|---|
| `NL2SQL_PLANNER_ENABLED` | **True** | bounded LLM planning before retrieval |
| `NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED` | **True** | structured clarification instead of guessing |
| `NL2SQL_WORKLOAD_HINTS_ENABLED` | **True** | mined join/filter hints in the SQL prompt |
| `NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED` | **True** | verified Q→SQL → few-shot corpus |
| `NL2SQL_CHART_SUGGESTION_ENABLED` | **True** | deterministic chart suggestions |
| `NL2SQL_JOIN_PARTNER_GUARANTEE_ENABLED` | **True** | pull missing join partners |
| `NL2SQL_EXPLAIN_FEEDBACK_ENABLED` | False | EXPLAIN scan assessment + rewrite history (needs EXPLAIN perms) |
| `NL2SQL_WORKLOAD_MINING_ENABLED` | True | offline mining job |
| `TOOL_SEARCH_ENABLED` / `TOOL_SEARCH_TOP_K` | True / 32 | selective schema injection |
| `EMBEDDING_LOCAL_FALLBACK_ENABLED` / `EMBEDDING_LOCAL_MODEL` | True / all-MiniLM-L6-v2 | local embeddings when no API key |
| `RL_BANDIT_ENABLED` | True | route bandit |
| `REWOO_ENABLED` | False | ReWOO light mode |
| `RED_TEAM_ENABLED` | False | scheduled red-team campaigns |
| `TURN_HEALTH_MONITOR_ENABLED` | True | fallback-turn monitor |
| `DATA_AGENT_SWARM_ENABLED` | False | swarm data pipeline |
| `LOOP_POSTMORTEM_ENABLED` | False | failure→lessons mining |
| `A2A_SELF_AGENT_ID` | zhanlu-platform | outbound identity header |

### 15.3 Embeddings without an API key

`memory_advanced/embeddings.py` falls back to an in-process
sentence-transformers model (`EMBEDDING_LOCAL_MODEL`, default
`all-MiniLM-L6-v2`; 384-dim, normalized, cached under a `local:` prefix)
with a 5-minute circuit breaker after API failures. Set
`EMBEDDING_API_KEY` (+ optional `EMBEDDING_API_BASE`) to use a hosted
model instead; for stronger Chinese, download `BAAI/bge-m3` and set
`EMBEDDING_LOCAL_MODEL`.

---

## 16. Key Dependencies

| Layer | Packages / services |
|---|---|
| Web/API | FastAPI, Pydantic, Uvicorn, httpx, SQLAlchemy 2 (Mapped), Alembic |
| Data | PostgreSQL (primary), MySQL/MSSQL/Oracle/SQLite connectors per dialect, Redis (cache, queues, locks), MinIO, ChromaDB, pgvector |
| LLM | OpenAI-compatible clients, vLLM (Qwen3-27B on-prem), Ollama; sqlglot (SQL parse/validate) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2 / BAAI/bge-m3), OpenAI-compatible API |
| Observability | OpenTelemetry SDK + OTLP exporter, structured logging, Prometheus-style in-process metrics |
| Agents/protocols | MCP SDK, custom A2A (JSON-RPC/SSE), custom ACP |
| ML (EDIA/research) | PyTorch, Transformers, statsmodels/XGBoost/LSTM, scikit-learn |
| Dev | pytest (+asyncio), Docker/Compose, GitHub Actions |

---

## 17. Technical Constraints & Known Limitations

1. **Single-process state.** Cancel flags (`run_control`), per-agent rate
   windows, bandit state (until bootstrapped), and in-process metrics are
   per-worker. Multi-worker/multi-node deployments need Redis-backed
   versions (Redis is already used elsewhere; the pattern exists).
2. **Full-suite OOM.** Running the entire pytest suite in one container
   process can OOM it — run targeted files (CI uses a curated list).
3. **Embedding model cache.** The local fallback model must be
   ST-compatible (`modules.json`). `BAAI/bge-m3` as cached is not; use the
   default MiniLM or pre-download a proper snapshot. Switching embedding
   models invalidates stored vectors (memory embeddings are plain float
   lists without versioning) — prefer a fresh window when changing.
4. **Mined-stats trust boundary.** Workload stats re-rank but never create
   candidates or grant access; the miner is regex-based (no full SQL
   parser) — complex statements may mine partial signals (bounded harm).
5. **EXPLAIN feedback is off** until EXPLAIN permissions on production
   warehouses are confirmed; only Postgres/MySQL/MariaDB supported.
6. **Guard caps are semantic decisions.** Per-tool caps (memory=3,
   clarify=3, …) trade freedom vs. runaway protection; changing them
   changes loop behavior and has dedicated tests.
7. **RL is advisory.** The bandit nudges routing; deterministic fallbacks
   always exist. RUNTIME_INFERRED signals are never persisted as truth.
8. **Dialect coverage.** Five SQL dialects modeled; EXPLAIN feedback and
   some rewriter rules are dialect-specific — new warehouses need the
   capability probe to pass before serving answers.
9. **Turn state scope.** `Nl2sqlTurnState` is per (conversation, KB);
   cross-conversation analytical state is out of scope by design (privacy).
10. **Scheduler loops are in-process asyncio tasks**, not Celery — a crashed
    process pauses schedules until restart (loops log and restart with the
    app).

---

## 18. How to Extend the System

- **Add a tool:** create `tool_handlers/<name>_tool.py`, call
  `registry.register(name, schema, handler, category, risk=…)`, import it
  in `tool_handlers/__init__.py`; add tests. Tools needing approval set
  `risk`/`dangerous_actions`.
- **Add an agent:** create an AgentApp (or builtin definition in
  `agent_definitions/`); configure `policy_profile` (risk tier, rate
  limit), toolset, KB bindings; publish via the lifecycle API.
- **Add a datasource:** onboard via the catalog indexer; run readiness +
  capability probes; the semantic store picks it up once KB-scoped.
- **Add a scheduled job:** implement a cycle in `services/`, register it in
  `scheduled_tasks.py` (`_periodic_task(...)`), add a config flag.
- **Add a model:** any OpenAI-compatible endpoint; register via the model
  router; capability probe handles dialect/tool quirks.
- **Golden cases:** add `AgentTestCase` rows; the golden suite + eval
  pipeline gate changes.

---

## Appendix A: Module Map (selected)

| Area | Files |
|---|---|
| Agent loop | `routers/agents.py` (v3 stream), `services/agent_loop/*` |
| Tools | `tool_registry.py`, `agent_tools.py`, `tool_handlers/*`, `tool_search.py` |
| Prompts | `agent_prompts.py`, `prompts/registry.py`, `agent_definitions/` |
| NL2SQL | `services/db/*` (39 modules; key: `semantic_store.py`, `table_retrieval.py`, `nl2sql_planner.py`, `grounding.py`, `nl_answer_service.py`, `execution_gate.py`, `dialect.py`, `sql_rewriter.py`, `query_log_retrieval.py`, `readiness.py`) |
| RAG | `document_ingestion/*` (embedder, agentic_rag, chunking), ChromaDB |
| Memory | `models/agent_memory.py`, `memory_advanced/*`, `case_based_reasoning.py`, `procedural_promotion.py`, `loop_postmortem.py`, `compaction/*` |
| Protocols | `a2a/`, `acp.py`, `mcp_client/`, `mcp_server/`, `routers/external_agents.py` |
| Multi-agent | `swarm/*`, `delegate_tool.py`, `context_bus.py`, `rewoo.py` |
| Learning | `rl/*`, `routing_feedback.py`, `agent_metrics.py`, `trajectory_reward.py` |
| Governance | `governance/*`, `permissions/*`, `agent_lifecycle.py`, `red_team.py`, `authz/*`, `run_control.py` |
| Observability | `tracing/*`, `harness/turn_recorder.py`, `agent_invocations.py`, `routers/admin_observability_traces.py`, `turn_health_monitor.py`, `scheduled_tasks.py` |

## Appendix B: Test Suite Map (selected)

| Suite | Covers |
|---|---|
| `test_tool_loop_guardrails.py`, `test_loop_guard_*` | loop-guard detection & no-leak |
| `test_nl2sql_*` (incl. `test_nl2sql_db_error_repair.py`, `test_nl2sql_oci_p2.py`) | NL2SQL stages, repair, parity |
| `test_rl_phases.py`, `test_hitl_reward.py` | reward, bandit, HITL, credit |
| `test_a2a_protocol.py`, `test_a2a_external_agents.py`, `test_acp_adapter.py`, `test_mcp_*` | protocols |
| `test_agent_lifecycle.py` | versioning/rollback/canary/catalog |
| `test_governance_gaps.py` | cancel, identity, tier enforcement |
| `test_parallel_tools_and_approval.py`, `test_permission_fail_closed.py` | approval semantics |
| `test_embedding_local_fallback.py`, `test_turn_health_monitor.py` | embeddings, monitoring |
| `test_swarm_*`, `test_fsm_*` | multi-agent + FSM routes |

---

*Maintain this document alongside `docs/plans/` and the memory index; when
a subsystem changes behavior, update its section and the flags table.*
