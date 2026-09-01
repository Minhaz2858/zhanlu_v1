# Zhanlu Agent Harness — Architecture & Behavior

This document describes how Agent harnesses work inside the Zhanlu platform:
the architecture, core components, the agent lifecycle (initialization →
execution → termination), communication protocols, integration points with
other Zhanlu modules, and concrete configuration examples with their
observed runtime behavior.

All file references are relative to `backend/` unless noted otherwise.

---

## 1. Architecture Overview

Zhanlu's agent system has **two parallel definition layers** that converge on
one shared runtime:

```
┌────────────────────────────────────────────────────────────────────┐
│                        CLIENTS                                      │
│   React SPA (Chat.jsx, StepsAgentBuilder)  │  Automations (cron)    │
└──────────────┬─────────────────────────────┬───────────────────────┘
               │ REST + SSE                  │ internal dispatch
┌──────────────▼─────────────────────────────▼───────────────────────┐
│                  FastAPI routers (app/routers/)                     │
│  agents.py (v2 buffered + v3 SSE chat) │ automation_api.py │ mcp.py │
└──────────────┬─────────────────────────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────────────────────────┐
│                     AGENT RUNTIME (the "harness")                   │
│                                                                     │
│  Definition layers                    Execution services            │
│  ┌──────────────────────────┐   ┌───────────────────────────────┐  │
│  │ AgentApp (DB, 30+ cols)  │   │ turn_action.py (forced tools) │  │
│  │  + 5-layer prompt        │   │ tool_registry.py (dispatch)   │  │
│  │  + Layer-3 harness fields│   │ iteration_budget.py           │  │
│  │ AgentDefinition (.md,    │   │ tool_loop_guardrails.py       │  │
│  │  YAML frontmatter, code) │   │ verification_stop.py          │  │
│  │  → BUILTIN_AGENTS        │   │ generation_orchestrator.py    │  │
│  └──────────────────────────┘   │ sub_agent_reliability.py      │  │
│                                  │ memory_manager.py             │  │
│                                  └───────────────────────────────┘  │
┌────────────────────────────────────────────────────────────────────┐
│  LLM access: llm_service.py → OpenAI-compatible API (DeepSeek),     │
│  provider_fallback.py / provider_health.py, model_router.py         │
└────────────────────────────────────────────────────────────────────┘
```

Key design decisions:

- **One runtime, two definition sources.** DB-backed `AgentApp` rows
  (user agents + seeded system meta-agents) and code-backed
  `AgentDefinition` objects (`BUILTIN_AGENTS`) both execute through the
  same tool-loop in `app/routers/agents.py`.
- **Tool-use is forced, not suggested.** `turn_action.py` computes a
  per-turn plan (grounding / file-generation / URL extraction) and forces
  `tool_choice` on iteration 0 so weak models cannot answer from memory.
- **Every turn is budgeted and guarded.** Conversation-level iteration
  budgets, a per-tool hard cap, and a verification-stop check bound every
  execution loop.
- **Governance is data, not code.** The Layer-3 "harness profile"
  (`manifest_json`, `policy_profile`, `output_contract`, …) rides on the
  agent row and is auto-derived at creation time.

---

## 2. Core Components

### 2.1 `AgentApp` — the DB-backed agent (`app/models/agent_app.py`)

The central entity. 30+ columns grouped as follows:

| Group | Fields |
|---|---|
| Identity | `name`, `description`, `project` (legacy string), `project_id` (FK), `is_system`, `resource_type` (`company`/`personal`), `role` (NULL = user agent; `automation_runtime` = hidden executor) |
| Capabilities/model | `capabilities` (JSON `string[]`), `model` (e.g. `gpt-4o-mini`, `automatic`), `agent_type` (`sequential`, …) |
| 5-layer prompt | `prompt_identity`, `prompt_boundary`, `prompt_reasoning`, `prompt_tools`, `prompt_output` |
| Bindings | `skills` (JSON `string[]` of skill **names**), `knowledge_bases` (JSON ids) |
| Topology | `topology` (`standalone`/`orchestrator`/…), `sub_agents` (JSON), `flow_mode`, `flow` (advanced mode) |
| Limits | `max_call_count` (default 50), `max_retries` (3), `max_iterations` (5) |
| Access flags | `data_read`, `data_write`, `human_fallback` |
| Observability | `trace_enabled`, `log_level` |
| Sampling | `temperature`, `top_p`, `max_tokens` |
| Lifecycle | `status` (`draft`/`active`/…) |
| Tool config | `tool_config` = `{"enabled_tools": [...], "disabled_tools": [...]}` |
| Prompt size | `progressive_disclosure` (default `True`: only skill name+description injected; body loaded on demand via `load_skill_body`) |
| **Layer-3 harness** | `manifest_json`, `data_bindings`, `skill_bindings`, `memory_scope`, `policy_profile`, `output_contract`, `evaluation_profile` |

> **Gotcha:** JSON columns (`capabilities`, `skills`, `knowledge_bases`,
> `sub_agents`) can round-trip as bare strings. Always normalize with
> `coerceStringArray` (`frontend/src/lib/jsonArray.js`) on both read and
> write boundaries.

### 2.2 The Layer-3 Harness Profile (`app/services/agent_tools.py:843`)

`_autofill_harness_profile()` derives the seven governance fields at
creation time from the agent's description, skills, and access flags. It
only fills fields the caller omitted; explicit values are never
overridden.

| Field | Derived default |
|---|---|
| `manifest_json` | `{agent_name, version, mission (first sentence of description), task_scope, boundaries.allowed/forbidden, risk_tier, created_by}` |
| `data_bindings` | One `{knowledge_base_id, access_mode: "read_only"}` per bound KB |
| `skill_bindings` | One `{skill_name, version: "latest", allowed: true}` per skill |
| `memory_scope` | `"app_shared"` (model default is `"user_only"`) |
| `policy_profile` | `risk_tier` = `high` if `data_write`, `medium` if `data_read`, else `low`; plus `requires_confirmation`, `max_concurrent_calls: 3`, `rate_limit_per_minute: 30`, `retention_days: 30` |
| `output_contract` | `allowed_artifact_types: [markdown, json, csv, text]`, `must_include_sources: true`, `citation_format: "inline"`, `max_response_length: 8192` |
| `evaluation_profile` | `trace_replay_enabled: true`, `grounding_checks: [source_citation, hallucination_check]`, `expected_accuracy: 0.85` |

If the LLM/caller provides no `tool_config`, `_create_agent()`
(`agent_tools.py:938`) resolves one from the agent's skills via
`resolve_tools_from_skills()` merged with `DEFAULT_USER_AGENT_TOOLS`, so a
new agent never starts with an empty toolset.

### 2.3 `AgentDefinition` / `BUILTIN_AGENTS` — code-backed agents
(`app/services/agent_definitions/__init__.py`)

File-based agent definitions (adapted from OpenHarness): a `.md` file with
YAML frontmatter (name, description, tools, denied_tools, model, effort,
permission_mode, max_turns, skills, hooks, color, background,
initial_prompt) whose body is the system prompt. Loaded into
`AgentDefinition` pydantic models; the built-in roster is `BUILTIN_AGENTS`
(line 869):

`explore`, `plan`, `worker`, `verification`, `data_agent`,
`forecast_agent`, `report_agent`, `orchestrator_agent`,
`perception_agent`, `rag_research_agent`, `diagnosis_agent`,
`pricing_agent`, `intelligence_agent`, `ecisco_bi_assistant`.

These are invoked **through delegation tools**, never as top-level chat
agents. `data_agent` is explicitly code-only (see
`system_agents.py` docstring).

### 2.4 Seeded system meta-agents (`app/services/system_agents.py`)

`ensure_system_agents()` idempotently seeds DB-backed system agents on
startup: `agent_builder`, `skill_agent`, `automation_agent`,
`general_assistant`, `power_user`. Each gets a focused `enabled_tools`
list intersected with the live registry (`_tools_in_registry`), plus the
shared `_BASE_HARNESS` Layer-3 baseline. `is_system=True` hides them from
user-facing lists, but the runtime still uses them — `general_assistant`
is silently auto-selected for any chat without a user-picked agent.

### 2.5 `ToolRegistry` (`app/services/tool_registry.py`)

Singleton mapping tool name → `ToolEntry`:

```python
@dataclass
class ToolEntry:
    name: str
    schema: dict            # OpenAI function-calling format
    handler: Callable       # (args, db, user_id, **ctx) -> dict; sync or async
    category: str           # also drives `toolset` grouping
    enabled_by_default: bool
    requires_env: list[str] # env vars needed; missing → structured
    check_fn: Callable      # availability probe (TTL-cached)
    max_result_size_chars: int | None
    is_async: bool          # auto-detected
```

Two access patterns: `get_tools(agent_app)` returns schemas for the
agent's enabled tools; `execute_tool(name, args, db, user_id)` dispatches
to the handler. Tools with missing config stay visible and return a
structured `missing_config` result so the agent can ask the user for the
missing values ("agent-handled missing config").

CRUD tools (`create_agent`, `update_skill`, `create_automation`, …) live
in `_CRUD_DISPATCH` (`agent_tools.py`) with schemas from
`_get_all_crud_schemas()` (`agent_prompts.py`); `_CRUD_TOOL_NAMES` in
`system_agents.py` keeps the registry filter from stripping them.

### 2.6 Execution guards

- **`iteration_budget.py`** — `IterationBudget(max_total)`; one budget per
  conversation, consumed once per loop iteration.
- **`tool_loop_guardrails.py`** — `_detect_tool_call_loop(messages,
  start_idx=...)` trips when the same tool is called
  `TOOL_CALL_HARD_CAP` times **within the current turn** (cross-turn
  repetitions like daily "Run Now" are legitimate and excluded).
- **`verification_stop.py`** — verification-based early stop checks.
- **`sub_agent_reliability.py`** — the same guards (budget, loop
  controller, retries, metrics) reused for sub-agent delegation loops.

### 2.7 Memory (`app/services/memory_manager.py`, `memory_advanced.py`)

`AgentMemory` rows scoped by `memory_scope`
(`user_only` / `app_shared` / `org_shared`). Semantic dedup (cosine ≥
0.85), consolidation (merge near-duplicates, increment `usage_count`),
and lifecycle (archive stale low-importance rows, promote frequently-used
ones) run as background tasks, not inline in the turn loop.

---

## 3. Agent Lifecycle

### 3.1 Initialization

Three paths produce a runnable agent:

1. **User-created via Agent Builder.** The `agent_builder` meta-agent
   calls the `create_agent` tool → `_create_agent()`
   (`agent_tools.py:938`):
   - derive `tool_config` from skills (fallback: baseline tools),
   - `_autofill_missing_fields()` — 5-layer prompt, capabilities, access
     flags,
   - `_autofill_harness_profile()` — the 7 Layer-3 governance fields,
   - insert `AgentApp` (defaults: `status="active"`, `model="automatic"`,
     `max_call_count=50`, `temperature=0.7`).
2. **Seeded system agents.** `ensure_system_agents()` at startup; tool
   lists filtered through the live registry so missing optional modules
   never break seeding.
3. **Builtin code agents.** Imported via `BUILTIN_AGENTS`; no DB row.

At chat time, initialization completes with:
- agent resolution (user-picked agent, else silent `general_assistant`),
- conversation context assembly (`synexia/context_assembler.py`),
- system prompt construction: 5 layers + progressive-disclosure skill
  summaries + date anchor + bound-KB data context,
- schema resolution: `tool_registry.get_tools(agent_app)`.

### 3.2 Execution (the tool loop)

Both chat endpoints share the same loop shape
(`routers/agents.py:2622` v2, `:5786` v3 SSE):

```
POST message
  └─► turn_action plan (forced tool_choice on iteration 0)
  └─► for iteration in range(MAX_TOOL_ITERATIONS):
        1. conv_budget.consume()          → exhausted? break
        2. steer_bus.drain(conv_id)       → inject mid-turn user steers
        3. _detect_tool_call_loop()       → tripped? nudge + break
        4. _compute_tool_choice(...)      → forced / auto / none
        5. LLM call (streamed or buffered, tools attached)
        6. no tool_calls?                 → final answer, break
        7. dispatch each tool call via registry / _CRUD_DISPATCH
           append tool results to message history
  └─► post-loop: generation_orchestrator fallback (artifact requests)
  └─► persist assistant message + tool results + trace/metrics
```

Notable behaviors inside the loop:

- **Forced grounding.** Precedence: bound-KB data question →
  `ask_data_agent`; file-format intent → `create_artifact`; URL present →
  `web_extract`; time-sensitive keywords → `web_search`; else `auto`.
  Forcing never requests a tool the agent hasn't been granted.
- **Loop-guard UX.** When the guard trips, the model gets an internal
  nudge ("use what you have, produce the final answer") while the user
  sees an agent-aware message — never raw scaffolding.
- **Token streaming (v3).** `STREAM_TOKEN_DELTAS` drives
  `_stream_llm_with_tools`, yielding deltas as they arrive while
  reassembling fragmented `tool_calls` for dispatch.
- **Delegation.** `ask_*` tools run a named `BUILTIN_AGENTS` member in a
  sub-conversation (`_run_sub_agent` in
  `tool_handlers/edia_delegation_tools.py`) with its own iteration budget
  and a `_DENIED_RECURSIVE` denylist preventing A→B→A delegation cycles.
  Return shape: `{success, answer, agent, iterations}`.

### 3.3 Termination

A turn ends when **any** of these fire:

| Condition | Mechanism | User-visible outcome |
|---|---|---|
| LLM returns no tool calls | natural stop | final answer streamed |
| Conversation budget exhausted | `IterationBudget.consume()` False | graceful wrap-up, logged |
| Same tool called ≥ `TOOL_CALL_HARD_CAP` in-turn | `_detect_tool_call_loop` | nudge + agent-aware fallback message |
| Verification stop | `verification_stop.py` | early final answer |
| Unhandled error | try/except around loop | SSE `error` event / logged; chat never silently dies |

Post-termination:

- **Artifact guarantee.** If the turn-action router saw a file request
  but the LLM produced neither a marker (`◤MD_DOCX◤`, `◤PPTX◤`, …) nor a
  successful `create_artifact` call, `generation_orchestrator.py`
  synthesizes a minimal payload and creates the artifact server-side —
  the user always gets a file or a clear, logged error.
- **Persistence.** Assistant message, tool results
  (`tool_result_persistence.py`), traces (when `trace_enabled`), and
  agent metrics (`agent_metrics.py`) are written.
- **Resume/steer.** Interrupted turns can be continued via
  `POST /conversations/{id}/resume`; mid-turn user input via
  `POST /conversations/{id}/steer` is drained on the next iteration.

---

## 4. Communication Protocols

### 4.1 Client ↔ Server

| Channel | Endpoint | Notes |
|---|---|---|
| REST (buffered) | `POST /api/apps/{app_id}/agents/conversations/v2/{conversation_id}/messages` | full response in one payload |
| SSE (streaming) | `POST /api/apps/{app_id}/agents/conversations/v3/{conversation_id}/messages/stream` | event stream, see below |
| Steer | `POST .../conversations/{conversation_id}/steer` | enqueue mid-turn user message |
| Resume | `POST .../conversations/{conversation_id}/resume` | continue interrupted loop |
| Permission mode | `PUT .../conversations/{conversation_id}/permission-mode` | `default`/`plan`/`full_auto` |
| CRUD | `GET/POST/PUT /api/apps/{app_id}/agents/conversations`, generic entity routes via `routers/entities.py` | tenant- and owner-scoped |

**SSE event types (v3):** token `delta`s, `activity_step` (per-tool
progress, e.g. "Listing running processes"), `phase` (Claude-style
headline), `steer`, `error`, plus keepalive comment pings every
`ZHANLU_SSE_HEARTBEAT_S` seconds (default 5) so long tool runs don't
drop the connection.

### 4.2 Server ↔ LLM

OpenAI-compatible chat-completions over HTTP (`llm_service.py`), with
function-calling tool schemas from the registry. Provider health is
tracked (`provider_health.is_healthy / record_success /
record_failure`), with fallback selection (`provider_fallback.py`) and
model routing (`model_router.py`); responses may be cached
(`llm_cache.py`) and retried (`llm_retry.py`).

### 4.3 Agent ↔ Tools

- **In-process dispatch**: `registry.execute_tool(name, args, db,
  user_id)` — sync handlers run directly, async handlers awaited.
- **Structured results**: every handler returns a dict; failures return
  `{"success": false, "error": ...}` or a `missing_config` payload rather
  than raising.
- **MCP**: external tool servers over `stdio`, `sse`, or `streamable`
  transports (`routers/mcp.py`, `mcp_oauth`), surfaced through the
  `mcp`/`mcp_tools_agent` path.
- **Sandbox**: code execution and skill runs go through sandbox jobs with
  their own SSE event stream (`routers/sandbox.py`).

---

## 5. Integration Points

| Module | How agents integrate |
|---|---|
| **Knowledge bases / RAG** | `knowledge_bases` ids → `data_bindings`; data questions forced to `ask_data_agent` (NL2SQL: `describe_schema`, `execute_query`, `answer_from_database`); hybrid retrieval in `retrieval_hybrid.py` |
| **Artifacts** | `create_artifact` tool + `◤…◤` markers + server-side fallback (`generation_orchestrator.py`); exporters for docx/pptx/pdf/html; governed by `output_contract.allowed_artifact_types` |
| **Automations** | Hidden `role="automation_runtime"` agent per tenant executes scheduled runs (`automation_executor.py`, `automation_dispatcher.py`); `create_automation`/`update_automation` CRUD tools; live execution SSE in `automation_api.py` |
| **Ecisco BI (EDIA port)** | 12 `ask_*` delegation tools (`tool_handlers/edia_delegation_tools.py`) gated by `ECISCO_BI_AGENT_ENABLED`; per-module flags (`PRICING_ENABLED`, `KNOWLEDGE_GRAPH_ENABLED`, …) in `app/config.py`; pipeline agents in `BUILTIN_AGENTS` |
| **Forecasting** | `forecast_discover/run/get/accuracy/rules/report/ppt` tool family + `forecast_agent` builtin |
| **Memory** | `memory` tool; `AgentMemory` scoped by `memory_scope`; background consolidation |
| **Skills** | `skills` ≠ tools: skills are name-referenced markdown bodies (progressive disclosure), resolved to tools via `resolve_tools_from_skills()` / `SKILL_DISPLAY_TO_TOOL`; lifecycle via `skill_agent`, Agent Studio (`agent_studio/`), `skill_factory.py` |
| **Sub-agents** | `sub_agents` list + `topology`; `delegate_task` for parallel subtasks; reliability guards shared via `sub_agent_reliability.py` |
| **MCP tools** | `mcp`, `mcp_oauth`, `mcp_oauth_manager` tools; `mcp_tools_agent` dispatch |
| **Auth/tenancy** | every route under `/api/apps/{app_id}/...`, `_apply_tenant` (org/app) + `_apply_owner` (created_by); system agents bypass owner filter |
| **Frontend** | MySpace `/my-space`, agent detail `/my-space/agent/:id` (`AgentConfig.jsx`); builder chat via `useAgentBuilder`; chat sessions via `ChatSessionContext` |

---

## 6. Configuration Examples & Resulting Behavior

### 6.1 Seeded system agent: `agent_builder`

From `system_agents.py` (`_build_system_agent_configs`):

```python
{
    "name": "agent_builder",
    "description": "Builds and configures new AI agents",
    "project": "global",
    "capabilities": ["agent_creation", "configuration"],
    "model": "gpt-4o-mini",
    "is_system": True,
    # tool_config = focused tool list ∩ live registry, plus CRUD names
    # harness: _BASE_HARNESS (trace on, memory_scope=app_shared,
    #          output_contract allows docx/pdf/pptx/html, …)
}
```

**Behavior:** hidden from user agent lists; reachable in the builder UI;
can call `create_agent`/`update_agent`/`list_tools` even though those
aren't registry tools (kept via `_CRUD_TOOL_NAMES`); cannot call
`create_artifact` (removed for `skill_agent`, and builder flows produce
agents, not files). When its tool loop trips the hard cap, the user sees
the builder-specific fallback: *"I'm going to build the agent with
sensible defaults now."*

### 6.2 User-created agent with auto-derived harness

Input to `create_agent` (via Agent Builder chat):

```json
{
  "name": "Pricing Analyst",
  "description": "Analyzes competitor pricing and recommends price actions. Reads ERP sell-out data weekly.",
  "skills": ["web_search", "forecasting"],
  "knowledge_bases": ["kb_erp_sellout"],
  "data_read": true
}
```

What the harness derives (`_autofill_missing_fields` +
`_autofill_harness_profile`):

```json
{
  "tool_config": {"enabled_tools": ["web_search", "forecast_*", "...baseline defaults..."]},
  "policy_profile": {"risk_tier": "medium", "requires_confirmation": true,
                     "max_concurrent_calls": 3, "rate_limit_per_minute": 30},
  "data_bindings": [{"knowledge_base_id": "kb_erp_sellout", "access_mode": "read_only"}],
  "memory_scope": "app_shared",
  "output_contract": {"allowed_artifact_types": ["markdown", "json", "csv", "text"],
                      "must_include_sources": true, "citation_format": "inline"},
  "manifest_json": {"mission": "Analyzes competitor pricing and recommends price actions",
                    "risk_tier": "medium", ...}
}
```

**Behavior at runtime:**

- First turn asking *"What did competitor X charge last week?"* →
  turn-action router detects time-sensitive keywords → forces
  `web_search` on iteration 0; the answer must cite sources
  (`must_include_sources`).
- A data question against the bound KB → forced `ask_data_agent`; the KB
  is read-only per `data_bindings`.
- All memories land in the app-shared scope, so other agents in the same
  app can recall them.
- Loop bounded by `max_call_count=50` conversation budget and the
  per-tool hard cap.

### 6.3 Builtin pipeline agent: `forecast_agent` (EDIA)

From `BUILTIN_AGENTS` (code-only, no DB row), invoked via:

```json
{"tool": "ask_forecast", "args": {"question": "Forecast SKU-123 sell-out for next week"}}
```

**Behavior:** `_run_sub_agent("forecast_agent", …)` builds the agent's
system prompt and tool schemas from its `AgentDefinition`, runs a
bounded sub-loop (`max_iterations=5`), with all recursive-delegation
tools denied (`_DENIED_RECURSIVE`), and returns
`{"success": true, "answer": "...", "agent": "forecast_agent",
"iterations": 3}` to the calling agent. The calling agent never sees the
sub-loop's intermediate tool chatter — only the final prose.

### 6.4 Automation runtime agent

Per-tenant hidden agent (`role="automation_runtime"`), created
automatically; never listed, never chat-able.

**Behavior:** when a scheduled automation fires, the dispatcher executes
the run through this agent's harness — same tool loop, budgets, and
guards as interactive chat — so automation output is indistinguishable in
quality and tracing from a manual run. Progress is observable via the
execution SSE stream in `automation_api.py`.

---

## Appendix — Key Files

| Area | File |
|---|---|
| Agent model | `app/models/agent_app.py` |
| Agent CRUD + harness autofill | `app/services/agent_tools.py` |
| System-agent seeding | `app/services/system_agents.py` |
| Builtin agent definitions | `app/services/agent_definitions/__init__.py` |
| Chat runtime (v2 + v3 SSE) | `app/routers/agents.py` |
| Turn-action router | `app/services/turn_action.py` |
| Tool registry | `app/services/tool_registry.py` |
| EDIA delegation | `app/services/tool_handlers/edia_delegation_tools.py` |
| Sub-agent reliability | `app/services/sub_agent_reliability.py` |
| Artifact orchestration | `app/services/generation_orchestrator.py` |
| Memory | `app/services/memory_manager.py`, `memory_advanced.py` |
| LLM access | `app/services/llm_service.py` |
| Feature flags | `app/config.py` (`ECISCO_BI_AGENT_ENABLED`, …) |
