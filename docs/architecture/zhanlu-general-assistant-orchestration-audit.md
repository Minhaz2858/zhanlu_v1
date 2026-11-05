# Zhanlu General Assistant — Orchestration Audit (P1 Investigation, Part A)

**Date:** 2026-10-31 · READ-ONLY investigation. Builds on `zhanlu-current-enterprise-agent-architecture-audit.md` (P0 audit — ReAct loop, delegation mechanics, ontology, NL2SQL are covered there and not repeated). Evidence convention: file:line = verified; "inferred" = clearly marked.

---

## 1. What General Assistant actually is

**A real, seeded `AgentApp` row marked `is_system=True`** — not a hardcoded router.
- Seeded at `backend/seed.py:233`; re-ensured idempotently at every startup (`system_agents.py:473-604`, refresh of tool_config/description/manifest + KB-binding sync :564-580). Hidden from My Space by `is_system` (:482-486).
- **Five-layer prompts are EMPTY on its row** — the layered assembly path (`agent_prompts.py:2442-2451`) never fires. Instead a **hardcoded branch** (:2428-2429) injects `GENERAL_ASSISTANT_SYSTEM_PROMPT` ("You are the General Assistant… full zhanlu toolset…") + skills block + tone/initiative/date blocks (:2466-2474). It is in `_DB_BOUND_SYSTEM_AGENTS` (:458-465) so it gets the schema-aware data protocol block.
- **Toolset** (`system_agents.py:378-403`): `web_search, web_extract, url_safety, x_search, agent_browser, memory, todo, kanban, session_search, read_file, write_file, image_generation, execute_code, delegate_task, ask_data_agent, sandbox tools, skill tools, clarify/approval tools, create_artifact, execute_automation,` the 8 `swarm_*` tools — filtered through the live registry.
- **Permission posture**: it IS in `SYSTEM_META_AGENTS` (`agent_tools.py:86`) → `requires_confirmation` is **bypassed for every tool** (:610-621); own `policy_profile` ships `risk_tier=low, requires_confirmation=False` (`system_agents.py:417-424`). It is **NOT** in `llm_router._SYSTEM_META_AGENTS` (`llm_router.py:44-48` = only agent_builder/skill_agent/automation_agent) → project/agent LLM bindings DO apply to it.
- Special-casing census (all behavioral branches, grep-complete over backend/app): the big one is `data_source_runtime.py:1397-1465` — project-selected → its KB bindings are **REPLACED** by the project's (:1400-1413); no project + none named in the message → **zero data access** anti-leak guard (:1445-1465); project named in message → that project's KBs replace (:1447-1463). Others: `delegate_tool.py:183,693` (default delegation target), `mixture_of_agents_tool.py:31`, v2/v3 fallback agent_name (`agents.py:7125,11647`), executions API default (`executions.py:32`).

## 2. The actual execution path for an enterprise question

User message → Chat API (v2 `agents.py:6896` / v3 SSE `:11920`) → routing pipeline:

1. **Template-intent routing** `_route_to_dedicated_system_agent` (`agents.py:6939` v2, `:11985` v3; detector `:3122+`): the **ONLY intent→agent rebind in the codebase** — three template intents (`automation_setup`, `agent_creation`, `skill_creation`), rebinds + persists `conv.agent_name`. A business question matches none → stays on general_assistant.
2. **Planning trigger** (`planning_trigger.py:169-243`): regex-score heuristic (threshold 0.6, short-message bypass), optional LLM escalation in the 0.2–0.6 gray band (`PLANNING_ROUTER_MODE`). This routes between ReAct and the FSM, **not between agents**.
3. **Follow-up refinement** detector (:367-402) → forces FSM.
4. **Data-bound override** (`agents.py:12618-12690` v3): bound data + not a non-data-intent → FSM; `_is_unambiguous_data_query` keyword classifier (`data_intent.py:24-82`). Inferred: "why is production below target?" likely falls through to the plain ReAct loop (no production/target terms in the classifier).
5. **ReAct loop with tools** — the question is answered by general_assistant itself using `ask_data_agent` (NL2SQL over bound KBs) and general tools.

**VERIFIED ABSENCE: there is no capability/intent classifier that maps a business question to a specialized agent.** Grep for `intent_to_agent|route_by_intent|match_specialist|capability_embedding` → 0 hits. The capability router has **no intent taxonomy** (its "intents" are output-format artifact intents, `capability_router.py:1309-1341`) and — critically — **the plan-DAG `"agent"` node type is declared in the docstring but has NO dispatch branch** (falls through to `_execute_tool_node`, :441-443). Multi-agent *decomposition* exists only as: the delegation nudge (`agents.py:13825-13837`, `delegation_nudge.py` — tells GA to fan out when the message has ≥2 parallelizable clauses) and batch `delegate_task(tasks=[…])` → `_delegate_batch` with `asyncio.gather` + concurrency 4 (`delegate_tool.py:203-259`) — **but batch tasks all go to ONE agent** (no per-task agent parameter, :203-211). Heterogeneous fan-out requires separate `delegate_task` calls (the tool executor runs parallel calls concurrently — P0 audit §7).

## 3. Agent discovery surface

`list_available_agents` (`delegate_tool.py:722-765`) — **exists and is real, but general_assistant does not have it in its toolset** (not in the seed list :378-403; only some agents like Production Scheduling carry it):
- Builtin definitions: `{name, kind:"builtin", description≤200, tools-count, source}`.
- Published AgentApps: `{name, kind:"user_agent", agent_id, description, capabilities}` — **no department, no project, no KB bindings, no status, no tool list**.
- Without this tool, GA picks sub-agent names from prompt knowledge or guesses.
- Builtin definitions live in `agent_definitions/__init__.py:738-874` (`general-purpose, explore, plan, worker, verification, data_agent, forecast_agent, report_agent`); `.md`-file definitions supported from `~/.zhanlu/agent_definitions/` (none exist in-repo).
- **Semantic/capability matching: none exists** (LLM eyeballs the description strings). The richer catalog primitives already exist but are disconnected from delegation: `AgentApp.capabilities` JSON, `AgentPortfolioEntry` (business owner/KPI/lifecycle, `models/agent_portfolio.py:28-49`), `GET /agents-catalog` (`routers/agent_lifecycle.py`), `MarketAgent`.

## 4. Delegation, failure and evidence handling

- Child result shapes (`delegate_tool.py:637-648`): `{"success", "response", "iterations", "truncated", "note"}` — wall-clock truncation is flagged, never faked. Batch aggregate `success = all(...)` (:275-279).
- **No parent-side code special-cases `success:false` or `truncated`** (grep-verified: agents.py references are labels/nudges only) — the parent LLM is trusted to re-plan. `DELEGATE_VERIFY_ENABLED=False` (LLM-judge, opt-in, `:152-173`).
- **Grounding guardrail is parent-loop-only**: `_check_hallucination_guardrail` (`agents.py:6332+`, v3 :15850) checks parent text against tool payloads — a child's `response` string IS a parent tool payload, so its figures are checked **as opaque text**; the child sub-loop itself runs loop-guard + budgets but **not** the figure-grounding guardrail. (`ask_data_agent`'s internal pipeline has its own gates — P0 audit §13.5.)

## 5. Project / department context

- Chat project: `AgentConversation.project_id` (FK, `agent_conversation.py:22-24`), re-resolved per message (`llm_router.py:92-139`), one-time heal (`agents.py:6954-6968`).
- Runtime effect of project selection (P0 audit + new detail): KB union (REPLACE for GA), project LLM override precedence System>Project>Agent (`llm_router.py:168-185`), access-policy resource = project share (`data_source_runtime.py:1657-1666`), memory stamped per project, project context block with up to 50 `ProjectMemory` entries (:1238-1287). **Artifacts have no project column** (verified).
- **Department does not exist as a model.** Only row-filter JSON examples mention it (`agent_data_binding.py:33`, `resource_access_policy.py:92`). Hierarchy today: `Organization → (flat users) → Project → resources`.
- Agent↔project is a **real M2M** (`ProjectAgent(project_id, agent_id, role primary|member)`, `project_agent.py:18-38`) + denormalized home FK; an agent CAN belong to multiple projects; one project can hold agents of any specialty (no constraints anywhere). Placement writes flow through the generic entity API (no dedicated router — partially verified).
- "All Departments" equivalent = `project_id=None` (ungrouped) → agent-default data scope; for GA that means **zero KB access** unless a project is named in the message (anti-leak guard).

## 6. Chat UI today

- GA is the **silent default**: if no agent resolves, `handleAgentSend` looks up `general_assistant` without pinning it in the UI (`Chat.jsx:1372-1389`).
- Project context is URL-driven (`?project=&projectName=`, :474-479, :664-684); **no department picker exists anywhere in frontend/src** (grep 0 hits); no in-chat project picker (set via links from Project pages/sidebar).
- Visible execution status: `plan_step_*` → live checklist (`streaming_plan_steps`, :2358-2380), `tool_progress` (:2382-2383), `activity_step` upserts (:2399-2411) — rendered by `LiveActivityStream.jsx`; delegate batch emits per-sub-agent activity_step pairs (`delegate_tool.py:214-236`).

## 7. Verified vs intended (bottom line)

| Capability | Status |
|---|---|
| GA as default conversational agent with data grounding (ask_data_agent→NL2SQL gates) | **IMPLEMENTED** |
| delegate_task (single + same-agent batch fan-out, 600s walls, truncation honesty) | **IMPLEMENTED** |
| Publishing + discovery data (published status, capabilities field, catalog API, portfolio) | **IMPLEMENTED (data) / DISCONNECTED (not in GA's toolset; payload too thin) |
| Intent→specialist-agent routing | **MISSING** (only 3 template intents) |
| Plan-DAG agent nodes | **DECLARED, NOT DISPATCHED** (`capability_router.py:441-443`) |
| Department concept | **MISSING** (no model; projects only) |
| Parent-side validation of child results | **MISSING** (judge flag off; grounding is parent-text-only) |
| Department picker / in-chat context switcher | **MISSING** (project via URL only) |
| Cross-department fan-out in one delegation call | **MISSING** (batch = one agent) |

**Answer to the ultimate question: NO — not yet.** GA can ground answers in bound data and can delegate, but it cannot *discover* suitable specialists (no discovery tool in its toolset), cannot *route by intent* (no mapping), has no *department context* to route within, and does not *validate* child results beyond opaque text matching. The precise smallest gaps are quantified in `zhanlu-enterprise-agent-discovery-and-routing.md`.
