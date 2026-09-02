# Zhanlu™ — Enterprise AI Operating System (Project Deep-Dive, REFRESHED)

> **Project:** Zhanlu (展鹭) Enterprise AI OS, built by SYNEXIA
> **Agent brain:** Synexia™ — the sole AI orchestration core
> **Repo:** `/Users/minhazulislam/Desktop/zhanlu_sep_1_n8n`
> **Snapshot date:** 2026-09-02 (refresh of the 2026-08-29 doc — supersedes it)
> **Scale:** ~227K LOC Python backend (app 226,641 + sandbox_worker 626), ~73.9K LOC React frontend (src), ~154K LOC backend tests, 227 markdown docs, 40 router files (31,771 LOC), ~85 SQLAlchemy models, 84 Alembic revisions (001–085, incl. two 057s), 904+ backend test files, 406 git commits. `n8n-master/` (255MB upstream n8n checkout) kept as parity reference for the Workflow subsystem.
> **Running branch:** `fix/automation-chat-artifacts-ordering` (62 dirty files — chat streaming/artifact-ordering work in flight; unrelated to this doc)

---

## Table of Contents

1. [What Zhanlu Is](#1-what-zhanlu-is)
2. [Product Rules](#2-product-rules-non-negotiable)
3. [Tech Stack](#3-tech-stack)
4. [Repository Layout](#4-repository-layout)
5. [Architecture Overview](#5-architecture-overview)
6. [Backend Deep Dive](#6-backend-deep-dive)
7. [Agent Runtime Deep Dive](#7-agent-runtime-deep-dive)
8. [Frontend Deep Dive](#8-frontend-deep-dive)
9. [Workflow (n8n-Parity) Subsystem](#9-workflow-n8n-parity-subsystem)
10. [Infrastructure & Deployment](#10-infrastructure--deployment)
11. [Data & Storage](#11-data--storage)
12. [Authentication & Security](#12-authentication--security)
13. [End-to-End Workflows](#13-end-to-end-workflows)
14. [Domain Subsystems](#14-domain-subsystems)
15. [Testing & QA](#15-testing--qa)
16. [Docs Archive & Feature Timeline](#16-docs-archive--feature-timeline)
17. [Known Issues & Gotchas](#17-known-issues--gotchas)
18. [How to Run](#18-how-to-run)

---

## 1. What Zhanlu Is

Zhanlu is an **enterprise AI operating system** — an industry-agnostic platform where users chat
with a main AI agent (the **Synexia** cognitive core) that plans, delegates to sub-agents, queries
connected databases through a governed datasource gateway, runs sandboxed code, and produces
**artifacts** (PPT/DOCX/XLSX/PDF/HTML dashboards, mini-apps) previewed inline in chat. Chinese
chemical/BI-industry-first in practice (Ecisco), generic in design. The September 2026 push adds an
**n8n-parity visual workflow builder** (canvas, merge/code/db_query nodes, Build-with-AI).

## 2. Product Rules (non-negotiable, from docs/00_FINAL_READ_ME_FOR_CODING_AGENT.md)

1. **Synexia is the only AI orchestration brain** — every model call flows through it.
2. Users chat with a **main agent**; main agents may delegate to sub-agents under Synexia control.
3. Users select high-level **capabilities**, not raw internal skills.
4. Users connect databases **once**; each agent uses only explicitly bound datasources (DATA-CORE-3).
5. Sub-agents inherit **no** database access by default (DATA-CORE-4).
6. All DB access goes through the **Datasource Gateway** and creates **DataSnapshots**; the sandbox
   never receives raw credentials (DATA-CORE-6).
7. Generated files are **Artifacts**, not chat attachments; inline preview is permission-checked.
8. **PostgreSQL is source of truth; Redis is temporary** (queues/cache/locks/events only).
9. Every agent is a **Harness Agent** — a versioned, governed execution profile (5-layer
   constitution), never a raw chatbot.
10. DB-agnostic tooling is a HARD RULE: zero hardcoded identifiers, standard SQL, `quote_ident()`.

## 3. Tech Stack

### Backend
| Area | Choice |
|---|---|
| API | FastAPI + Uvicorn, single `backend/main.py` (904 LOC); no auth middleware — per-route `Depends` |
| ORM/migrations | SQLAlchemy 2.0 + Alembic (84 revisions) **plus** boot-time auto-migrations in main.py (dual system) |
| Stores | PostgreSQL 16 (truth), Redis 7 (queue/cache/locks/fanout), MinIO (mostly idle — default `postgres_bytea` artifact backend), ChromaDB (embeddings) |
| Config | pydantic-settings `config.py` — 1,508 LOC, ~182 feature booleans, flag-OFF-by-default convention |
| Auth | PyJWT access + rotating SHA-256-hashed refresh tokens, OTP registration, OIDC/SSO module |
| LLM | OpenAI-compatible `llm_service.py`; provider fallback/health; task-based model routing; `.env` → DeepSeek cloud (`deepseek-chat`); local qwen3.5-27b documented |
| Sandbox | `sandbox-worker` (only process with docker.sock): `--network none`, cap-drop, Unix-socket LLM proxy as sole egress |
| Docs gen | python-docx/pptx, openpyxl, reportlab, mammoth, LibreOffice headless previews |
| NL2SQL | sqlglot validation + SQLAlchemy adapters; semantic catalog + schema linking ON |
| Forecasting | XGBoost, statsmodels, VAR/VECM, Chronos-Bolt/Moirai, Optuna, conformal intervals (~60 flags) |
| Protocols | MCP 2026 (server wraps ~172 tools at /api/ext-mcp/mcp), A2A (JSON-RPC at /a2a), OTel GenAI spans |

### Frontend
| Area | Choice |
|---|---|
| Framework | React 18 + Vite 6, plain JS (no TS in src), react-router-dom v6 |
| SDK shell | `@base44/sdk` legacy (Proxy-wrapped, 401-retry) + native REST wrappers on `authFetch` |
| UI | Tailwind 3 (warm-paper + burnt-orange HSL tokens, shadcn map), Radix primitives, framer-motion, lucide |
| Charts/canvas | recharts; @xyflow/react v12 (workflow canvas) |
| Data | @tanstack/react-query, react-hook-form, zod |
| Testing | 135 colocated Vitest files (54 are source-contract regex pins); Playwright e2e |

## 4. Repository Layout

```
zhanlu_sep_1_n8n/
├── backend/
│   ├── main.py               # create_app, lifespan chain (migrate→seed→agents→routers), ~30-entity CRUD loop
│   ├── app/
│   │   ├── config.py         # 1,508-line Settings (~182 bools) — doubles as feature changelog
│   │   ├── deps.py           # get_db / get_current_user_required|optional / require_admin
│   │   ├── models/           # ~85 model files (~117 SQLAlchemy classes incl. base)
│   │   ├── schemas/, repositories/, middleware/, domain_configs/, dashboards/ (generated app code)
│   │   ├── routers/          # 40 files / 31,771 LOC — agents.py alone 18,311 (58%)
│   │   └── services/         # 64+ service dirs: agent_loop, synexia (FSM), harness, swarm,
│   │                         #   agent_studio, tool_handlers (97 files), memory*, llm_service,
│   │                         #   mcp_client, mcp_server, a2a, computer_use, sso, tracing, ...
│   ├── sandbox_worker/       # Redis BRPOP → docker run sandbox executor
│   ├── alembic/versions/     # 84 revisions 001→085 (two 057s; 018/019 skipped)
│   ├── system_skills/        # 3: agent-builder-principles, harness-creation-rules, using-superpowers
│   ├── skills/               # 57 bundled skill dirs
│   └── tests/                # ~904 test files / ~154K LOC
├── frontend/                 # ~73.9K LOC src; dist/ (nginx :ro bind-mount); base44/ legacy
│   └── src/                  # pages, components (chat/, workflow/, ui/, skillagent/), api/, lib/, hooks/
├── n8n-master/               # 255MB upstream n8n checkout — parity reference ONLY (not built/run)
├── docs/                     # 227 .md: 00–09 numbered spec dirs, plans/ (41), superpowers/ (67 plans + 65 specs)
├── scripts/                  # 58 ops/dev scripts; Makefile targets up/down/db-*/smoke
├── deploy/                   # frontend.nginx.conf + installer-generated override (STALE vs root)
├── docker-compose.yml        # backend, sandbox-worker, postgres, redis, minio, 4 sandbox image builders
├── docker-compose.override.yml  # ACTIVE override — adds frontend :8088 (root one wins over deploy/)
├── .env / .env.example / .env.production
├── PROJECT.md                # THIS FILE (refresh 2026-09-02); PROJECT copy.md = stale 08-29 duplicate
├── ZHANLU_FINAL_SPEC*.md     # context-safety specs (near-identical pair)
└── agent_qa/, data/, backups/, .codebuddy/memory/
```

## 5. Architecture Overview

Seven conceptual layers (docs/07_existing_architecture): identity/tenancy → Synexia cognitive core
→ agent harness → memory/knowledge → execution (sandbox/automations) → platform services
(artifacts/dashboards/market) → protocol surface (REST + SSE + MCP + A2A).

One runtime, two agent-definition layers: DB-backed `AgentApp` rows (5-layer constitutional prompt,
Layer-3 harness JSON auto-derived) and code-backed `AgentDefinition`/`BUILTIN_AGENTS` (.md + YAML)
— both execute through the same guarded tool loop in `routers/agents.py`.

**Two execution runtimes coexist:**
- **Legacy ReAct loop** (the one actually running — see §7): `agents.py` v2 buffered + v3 SSE chat,
  forced tool-choice via `turn_planner.py`, guardrails from `agent_loop/`, iteration budgets.
- **SynexiaFSM** (12-state, shipped default-OFF in .env): `services/synexia/fsm.py` —
  INIT→GOAL→CONTEXT→PLAN→GATE→ACT→OBSERVE→VERIFY→FINALIZE→QUALITY_EVAL→DONE/FAIL plus
  reflexion/verifier/quality_eval/eval_harness. Entry `chat_loop_adapter.run_via_fsm()` falls back
  to the legacy loop on any error. `.env` sets `SYNEXIA_FSM_ENABLED=false` → the FSM is currently
  **dark code in production**; the active chat path is the legacy loop with turn planning.

## 6. Backend Deep Dive

### Entry & wiring (main.py)
- Lifespan (main.py:485-787): long try/except "non-fatal" chain — create_all + runtime
  auto-migrations, hooks loader, marketplace sync, system-agent seeding (system_agents.ensure_system_agents),
  super-admin, LLM catalog seed, prompt normalization, skills registry, scheduled tasks, automation
  dispatcher + zombie reaper, workflow scheduler, data-execution cleanup, dashboard_app manager.
- Router mounting (main.py:811-873): auth/auth_sso/public/diagnostics under `/api`; then a generic
  **entity CRUD loop over 30 models** (`/api/apps/{app_id}/entities/<Entity>`); a2a mounted at root
  (`/a2a`, `/agent-card`); sub-apps `/api/ext-mcp` (FastMCP) + `/api/uploads` (StaticFiles).

### config.py — the important flag groups
- Core loop: `TURN_PLAN_ENABLED=True` (default), `AGENT_FAST_MODE_ENABLED=True`,
  `AGENT_FAST_GOAL_CONTRACT_ENABLED=False` ("was True"), `HIERARCHICAL_LLM_ENABLED=True`.
- Synexia FSM: `SYNEXIA_FSM_ENABLED=True` config default BUT **.env forces False**;
  `SYNEXIA_QUALITY_GATE_ENABLED=True`, `SYNEXIA_QUALITY_EVAL_ENABLED=True`, `SELF_EVAL_LLM_GATE_ENABLED=True`.
- Goal/intent: `GOAL_CONTRACT_ENABLED=False`, `INTENT_PLANNER_ENABLED=False`, `DELIVERABLE_PHASE_LOCK_ENABLED=False`.
- PPT deck: rich suite — `PPT_AUDIT_BLOCKING_ENABLED=True` (+ audit off), `DYNAMIC_DOCUMENT_PLANNING_ENABLED=True`.
- Harness/tracing (all OFF): `AGENT_HARNESS_ENABLED=False`, `AGENT_TRACING/CHECKPOINTING=False`,
  `TRACING_PROVIDER="noop"` (otel selectable), `TRACE_METRICS_ENABLED=False`.
- Sandbox: `SANDBOX_ENABLED` config False / **.env True**; `--network none`; Unix-socket LLM proxy on.
- Integrations (implemented, OFF): `SSO_ENABLED=False`, `COMPUTER_USE_ENABLED=False` (+ URL allowlist),
  `A2A_ENABLED` absent → off; MCP marketplace flags; `DEEP_RESEARCH_ENABLED` config False / **.env True**
  (v1 loop) while `DEEP_RESEARCH_MODE_ENABLED=False` (harness-gated P3 variant).
- EDIA domain gates mostly OFF: forecasting master False but ~30 `.env` overrides True
  (foundation models, stacking, champion/challenger); NL2SQL semantic catalog + schema linking True,
  shadow mode False; dashboards `FULLSTACK_DASHBOARD_ENABLED=False`.

### Router inventory (40 files, 31,771 LOC)
- **agents.py — 18,311 LOC, the monolith** (13 routes, ~146 inner defs): conversation CRUD + v2
  buffered chat + v3 SSE streaming + the entire tool loop + `_run_sub_agent`. 58% of router LOC.
- Tier 2: artifacts.py (1,425), automation_api.py (1,329), project_catalog.py (675), functions.py
  (658), dashboards.py (649), knowledge_bases.py (520), mcp.py (504), llm.py (497), workflows.py (487).
- Domains: auth (auth, auth_sso, llm, public, diagnostics); agents/chat (agents, agent_runs,
  executions, agent_studio, skill_studio, chat_tools, project_memories); artifacts/files (artifacts,
  sandbox, data_snapshots); dashboards (dashboards, analytics, functions); KB/data (knowledge_bases,
  nl2sql); workflows/automation (workflows, automation_api, hooks, app_logs); admin/governance
  (admin_users/evals/invocations/observability_traces, governance, workspace_settings,
  access_policies, resource_shares, marketplace); integration (mcp, a2a, integrations, openharness).

### Newer execution services
- `agent_loop/` (~1,250 LOC): fsm_pruner (caps/prunes context between FSM states),
  guardrails (force-finish, tool caps, wrap-up nudge, pause_for_approval), tool_executor
  (batch execution, progress events while waiting), fallbacks (artifact/data-summary aware),
  streaming_helpers + sse_builders. Lazily imported from agents.py to dodge circulars.
- `harness/` (gated OFF): orchestrator (DI-based AgentRunOrchestrator), run_service (+ background
  poller), checkpoint_sink, tracing_sink — real, waiting on `AGENT_HARNESS_ENABLED=True`.
- `turn_planner.py` (441 LOC, ON): kinds none/data/dashboard/report/generic; typed live_events →
  PlanChecklist survives reloads. `turn_action.py` is the older forcing router.

### Tool registry & security
- `tool_registry.py` (695 LOC): singleton registry name→ToolEntry (schema/handler/category/toolset/
  requires_env/check_fn/max_result_size). Registration by **import side effect** across 97
  tool_handlers files / 28,448 LOC (~140 register sites; FastMCP exposes ~172 incl. CRUD extras).
- Groups: Fusion360 CAD family (~48 tools across 10 modules) · dashboard_tools (7) · browser
  (web_search/extract + agent_browser) · delegation (delegation_tools.py 1,778; delegate_tool.py;
  sub_agent_reliability.py) · db_tools (1,315) · sandbox_tool/code (1,464) · forecast · MCP
  (mcp_tool/proxy/oauth) · skills platform (8 modules) · comms (send_message, discord, feishu,
  microsoft_graph, homeassistant, x_search, xai_http, openrouter_client) · media (image/video gen,
  tts, transcription, vision, voice_mode) · security/utility (approval, clarify, interrupt,
  checkpoint_manager, session_search, cronjob, kanban, mixture_of_agents, swarm_orchestrate…).
  Agent-CRUD tools live OUTSIDE the registry in `agent_tools.py` `_CRUD_DISPATCH`.
- `tool_security.py`: SSRF (blocks private/link-local/CGNAT/169.254.169.254 incl. v4-mapped-v6),
  path traversal, output truncation, secret redaction, prompt-injection scan on memory content.

### Swarm & delegation
- `swarm/`: orchestrator (retries/escalation), pool (FanOutPool: semaphore + cost ledger +
  wall-clock, `SWARM_MAX_CONCURRENT=8`), runtime (SwarmRuntime.run), mailbox, team_registry
  (persisted Team + swarm_mailbox). Exposed as `swarm_orchestrate` tool.
- Memory: three layers — `AgentMemory` rows (user_only/app_shared/org_shared),
  `memory_advanced/` (pgvector w/ numpy fallback, SHA-256 dedup, TTL, importance, LLM auto-extract),
  `memory_manager.py` (cosine≥0.85 dedup + consolidation, background; OHMO consolidation ON).

### Integration modules — implemented vs stub (all verified 2026-09-02)
A2A: real (AgentCard GET /a2a/agent-card; JSON-RPC tasks/send|get|cancel; A2aTask model; outbound
client; migration 085). MCP server: real (FastMCP streamable-HTTP, mounts at /api/ext-mcp/mcp).
MCP client: real (stdio + streamable-HTTP + OAuth PKCE + session mgmt + marketplace registry;
migration 084). Computer use: real facade over Rust agent-browser CLI (open/screenshot/get_state/
click/type/scroll/close; per-conversation session TTL 1800s; URL allowlist) — **no vision loop yet**
(blocked by vision-model routing; see design docs/superpowers/specs/2026-09-02-agent-runtime-vision-voice-design.md).
SSO/OIDC: real (discovery + JWKS verify + state store + account linking; SSO_ENABLED=False).
Tracing: dual-provider (noop/otel) + GenAI semantic-convention spans + Admin traces endpoints (OFF).
Deep research: v1 loop implemented + DEEP_RESEARCH_ENABLED=true in .env; harness-gated mode OFF.
Notifications: gateway enabled in .env but **email-only** (no channel providers yet).

## 7. Agent Runtime Deep Dive

This is the heart of the platform — everything a user-created agent inherits.

- **Two definition layers, one loop** (docs/agents/agent-harness.md, authoritative):
  - DB `AgentApp` (~30 cols): identity/project/org; capabilities; **5-layer prompt**
    (identity/boundary/reasoning/tools/output); skills[] + knowledge_bases[] bindings; topology
    (standalone/orchestrator/…) + sub_agents[] + flow[]; limits (max_call_count 50, max_retries 3,
    max_iterations 5); Layer-3 harness JSON (manifest/data_bindings/skill_bindings/memory_scope/
    policy_profile/output_contract/evaluation_profile) auto-derived at creation.
  - Code `AgentDefinition`/`BUILTIN_AGENTS` (.md + YAML frontmatter) — roster: explore, plan,
    worker, verification, data_agent, forecast_agent, report_agent, orchestrator_agent,
    perception_agent, rag_research_agent, diagnosis_agent, pricing_agent, intelligence_agent,
    ecisco_bi_assistant. Seeded system meta-agents: agent_builder, skill_agent, automation_agent,
    general_assistant, power_user (system_agents.py); hidden `automation_runtime` executor per tenant.
- **Agent Studio** (creating new agents): chat builder (AgentBuilder page, `agent_builder` system
  agent) + StepsAgentBuilder; AgentConfig editor with 5-layer form, topology tree, sub-agent nesting,
  per-field auto-save, duplicate; preflight.py (PreflightService.check_agent + dry_run),
  review_queue.py (SkillCandidate → approve → SkillProfile publish), skill_factory.py (LLM
  generation + deterministic fallback). system_skills/agent-builder-principles.md enforces:
  never a raw chatbot, verify-before-claim, one question per turn, `update_agent` shape
  `{agent_id, fields}`.
- **The active loop** (legacy ReAct, since SYNEXIA_FSM_ENABLED=false): turn_planner forces
  tool_choice (grounding/file-gen/URL-extraction) on iteration 0; per-conversation iteration
  budgets; same-tool hard cap (tool_loop_guardrails); verification-stop check; agent_loop
  guardrails + fsm_pruner context pruning; fallback generators guarantee an artifact/data summary
  post-loop; v3 SSE events (phase_enter, plan_step_added/completed, tool_call_started/finished,
  subagent_invoked/returned) stream to LiveActivityStream and persist as typed live_events so the
  PlanChecklist survives reload.
- **The FSM** (dark): 12 states + reflexion (LLM-rubric, gated), verifier (deterministic, always-on),
  quality_eval (semantic completeness + critique w/ text-only regeneration),
  eval_harness (golden-scenario JSON runner), eval_ci, smart_retry, confidence_scorer,
  adaptive_planner, capability_router, intent_router, plan_dag, context_assembler, finalize,
  policy_evaluator. `SYNEXIA_FSM_ENABLED=true` is one .env flip away — LEGACY_REACT_LOOK=1 is the
  rollback shape.
- **Known runtime gaps (verified 09-02):** vision tool posts to `settings.LLM_MODEL` (deepseek-chat
  text-only) — no VISION_MODEL routing; voice-out (useTTS) is dead code — zero importers;
  notifications unused; no global history search; no session auto-title; computer-use vision loop
  missing. Spec: docs/superpowers/specs/2026-09-02-agent-runtime-vision-voice-design.md (commit fd6cf7c).

## 8. Frontend Deep Dive

- **Shell:** App.jsx picks desktop vs mobile tree (forceMobile/device). Desktop: ProtectedRoute →
  AppLayout (Sidebar + Outlet + PageErrorBoundary + AutomationExecutionStatusDrawer); mounts
  PersistentStreamProvider > ChatSessionProvider ABOVE the router so streams/session survive
  navigation. Lazy routes: chat /, /chat, /automation/chat (all Chat.jsx); automation list/detail;
  my-space (MySpace, ProjectDetail, AgentConfig, WorkflowCanvas, ResourceDetail); market; toolkit;
  skill-agent; /agent-builder; settings; admin/users + admin/observability; dashboard/:id.
- **Chat.jsx — 3,573-line monolith.** handleSend: ensure ChatSession → create user ChatMessage →
  agent resolution (silent auto-select general_assistant; automation_agent for automation origin) →
  handleAgentSend (v3 `/agents/conversations/{id}/messages/stream`, SSE \n\n-split, delta/tool_calls/
  activity/plan events, persists partial content, generation-counter guards against stale loops);
  legacy tail (raw InvokeLLMStream) only for agent-less turns. Renders **two ChatInput instances**
  (empty-state + composer) — drift hazard.
- **Context state:** URL params (?conv/?project/?projectName/?agentName) are the cross-component
  bus, written via history.replaceState — dozens of regression tests pin the contract.
  ChatSessionContext: sessions/activeId/pendingProject + chatGeneration monotonic counter.
  PersistentStreamContext: streams outlive Chat, abortRef + ownership checks, pendingMessageRef per
  session, MAX_CONCURRENT_RUNS=3, run-status registry → sidebar spinners.
- **LiveActivityStream** (1,217 LOC): typed SSE event feed — phase headlines, PlanChecklist
  (rebuilt from persisted events), tool rows with data preview, subagent nest/retry indent,
  RunningTimer, auto-collapse; legacy adapter synthesizes events from activity_steps.
- **MessageBubble** (659 LOC): markdown preprocess, artifact partition (inline dashboard preview vs
  artifact cards), attachment chips, web-source chips, actions (copy/share/feedback/export).
- **Uploads:** handleUploadFile → base44.integrations.Core.UploadFile → optimistic chip →
  UserFile.create (kind by extension); paste-to-upload images; FilePreviewPane/FilePreviewer
  dispatch DOCX (docx-preview + mammoth fallback) / XLSX / PDF / PPTX / HTML; ArtifactCanvasEditor.
- **Workflow canvas:** pages/WorkflowCanvas.jsx (controlled @xyflow/react v12): undo/redo (100 cap),
  clipboard copy-paste via buildPasteGraph, keyboard shortcuts, validate/save/publish/run,
  n8n Error-Workflow picker, Build-with-AI slide-over. See §9.
- **Agent surfaces:** AgentConfig (full 5-layer editor w/ topology tree + flow mode),
  AgentBuilder (chat builder, ?prefill=), SkillAgent (/skill-agent chat studio bound to skill_agent
  + ScrapeUrlDialog + GitHub collect), SkillExecutions, Market (marketplace), MySpace (tabs:
  project/agent/kb/dashboard/workflow/mcp), KbSetupDialog (db/file/file_db sources; CSV/XLSX →
  sqlite), ProjectDetail (1,907 LOC — agents/resources/chats/memory/access/policy sections).
- **API:** two-track — base44/sdk Proxy with 401-retry + rotateAccessToken (legacy entities),
  and REST wrappers on authFetch (workflows.js, mcp.js, dashboards.js, marketplace.js,
  skillStudio.js, agentEnhanced.js stream generator, agentInvocations, agentFeedback, accessPolicy).
  SSE reader loops are hand-rolled per consumer (Chat.jsx legacy, agentEnhanced, RunDrawer, mobile) —
  duplicated, drifts.
- **Design:** warm-paper bg + burnt-orange primary HSL tokens (--primary 25 65% 46%), Georgia
  display serif, ~60 shadcn/Radix ui components, sonner toasts, dual-language strings (zh-first),
  useScreenSize density tiers.
- **Testing:** 135 colocated Vitest files (components/chat 39, pages 34, lib 23, dashboard 8…);
  54 are source-contract regex pins (readFileSync their own source) — brittle by design.
- **Dead/dup surfaces:** pages/AdminConsole.jsx + components/workflow/NodeSettingsDrawer.jsx
  unrouted/unimported; mobile chat page is a parallel stream loop reusing MessageBubble/ChatInput
  but not Chat.jsx (v3-event drift risk).

## 9. Workflow (n8n-Parity) Subsystem

September 2026 push ("like n8n and much more better"), driven against n8n-master/ reference:
- Backend: workflows.py router (list/get/save/validate/run/run-async/streamUrl/publish/delete +
  fetchNodeTypes under /api/apps/{appId}/workflows); WorkflowRun/WorkflowRunStep models
  (migration 083); sandboxed **Code node** executes in Docker via sandbox-worker (job events,
  result.json capture); **Merge node** appends parallel-branch items (n8n parity);
  **db_query node is NL2SQL-first** with a non-IT form (mode:'nl' default; visible_when/Advanced
  sections; single-connector auto-select); workflow_builder system agent designs 3-20 node graphs
  with decision-summary drafts; Build-with-AI session resume per workflow (workflow_id filter on
  list_conversations); schema/edge-id normalization fixes (null-id edges, stable edge ids).
- Frontend: controlled canvas page + node palette (left rail, search/group), registry-driven node
  types (lib/nodeTypeRegistry.jsx caches fetchNodeTypes; workflowNodeTypes.jsx renders 19 types
  via ONE generic NodeShell with typed port handles; NODE_DEFS static fallback catalog),
  NodeDetailView (NodeSettingsDrawer dead), RunDrawer (authStreamingFetch on streamUrl, n8n push
  pattern), Build-with-AI slide-over chat, error-workflow picker, ⌘Z/⌘Y/copy/paste/validate/save/
  publish/run top bar.
- Status: active area of the repo (most recent commits); Code node hidden from palette by choice
  ("keep the picker simple").

## 10. Infrastructure & Deployment

- Compose services: backend (zhanlu-backend, :5002, 4g mem, ./backend:/app bind, prestart.sh =
  wait → alembic upgrade → seed → uvicorn, UVICORN_RELOAD=false), sandbox-worker (docker.sock only
  service, 512m, DooD via ./data/sandbox_tmp bind), postgres:16 (:5400), redis:7 (:6300),
  minio (:9400/:9401 + minio-init bucket), 4 one-shot sandbox image builders; frontend added by
  ROOT docker-compose.override.yml (nginx:alpine :8088, dist :ro + deploy/frontend.nginx.conf,
  proxies /api → backend). Two networks (app_net/data_net).
- **Deploy rule (HARD):** every backend deploy restarts BOTH zhanlu-backend AND zhanlu-sandbox-worker
  — bind-mount never reloads imported modules; --reload previously killed in-flight runs.
- .env (root, 370 lines): DeepSeek cloud (OPENAI_BASE_URL=https://api.deepseek.com/v1,
  LLM_MODEL=deepseek-chat); Ollama local qwen3.5-27b commented; MODEL_CONTEXT_WINDOWS map;
  ~90 flags flipped true (deep research, notifications gateway, forecasting suite, semantic
  catalog, PPT suite, EVAL_PIPELINE + sample 0.1…); secrets mixed plaintext/truncated/masked;
  SUPERADMIN_PASSWORD=admin123 (bootstrap). .env.production third un-diffed config.
- Sandbox worker mechanics: producer SandboxService RPUSHes sandbox:queue → worker BRPOP →
  SandboxJob running → docker run (network none, read-only, cap-drop ALL, mem/cpu/pid caps,
  Unix-socket LLM proxy is the only egress for --network none) → outputs collected → completed/
  failed + Redis pub/sub sandbox:events:{job_id}; in-process fallback when no docker.sock;
  image map: pptx→sandbox-pptx, docx/xlsx/pdf→office, md/html/chart→python, skill→zhanlu-sandbox-skill.
- MinIO is mostly decorative: ARTIFACT_STORAGE_BACKEND=postgres_bytea default → artifacts in PG
  bytea; infra/minio lifecycle JSONs = unused intent.
- Makefile: up (builds sandbox images), down, reset, db-check/setup/reset/info (docker compose run
  db_manage), smoke (scripts/smoke_e2e.sh), smoke-ui (Playwright). scripts/ = 58 files.

## 11. Data & Storage

- Role split: PostgreSQL = truth (SQLAlchemy; required — no silent SQLite default);
  Redis = sandbox:queue, sandbox:events:{job_id}, LLM cache, locks; MinIO = idle; ChromaDB =
  embeddings (chat-upload RAG, semantic catalog, schema linking).
- **Two schema systems (drift risk):** 84 Alembic revisions (001–085, two 057s, no 018/019) PLUS
  boot-time auto-migrations in main.py (`_specific_migrations`/`_run_auto_migrations` — ALTER ADD
  COLUMN IF NOT EXISTS on ~20 tables, org_id/app_id tenant sweep on every table, project_id
  backfill). Known drift: agent_memories.project_id added to model with no migration.
- Alembic theme (recent): 075 chat_messages_live_events, 076 data_execution, 077 session_state,
  078 agent_invocation_observability, 079 chat_messages_sources, 080 otp_purpose,
  081 chat_messages_attachments, 082 chat_shares, 083 workflow_runs, 084 mcp_servers_columns,
  085 a2a_tasks.
- Model domains (~85 files): tenancy (user, organization, app_workspace, workspace_settings,
  refresh_token/revoked_token/otp_code…); agents (agent_app, agent_conversation, agent_memory,
  agent_run(+step), agent_invocation, agent_todo, bindings, market_agent, project_agent,
  project_memory, project); chat (chat_session, chat_message, chat_share, session_state);
  execution (execution, automation_task/execution/file, workflow_run, data_execution, eval_result,
  audit_event, artifact_event); artifacts (artifact + build manifest, context_manifest,
  cad_build_contract); dashboards (dashboard, dashboard_app, dashboard_version); knowledge
  (knowledge_base, catalog, semantic_mapping, response_cache_entry, q_sql_example, nl2sql_query_log);
  skills (skill_source/profile/candidate/run/test_case, marketplace_skill/rating, learning_proposal);
  mcp/a2a (mcp_server, tool, llm_model, a2a_task); governance (resource_access_policy, registry,
  share, hook_rule, policy/approval/cost via services); forecasting (20 tables); misc
  (swarm_mailbox, decision_flow, report, intelligence, experience_entry).

## 12. Authentication & Security

- Email+password (bcrypt) → JWT access (HS256, jti, default 15 min but JWT_EXPIRE_HOURS=24 env)
  + rotating refresh (SHA-256 hash stored; 30-day TTL; logout revokes access jti + all refreshes).
  Registration via email OTP (purpose column since 080); password reset. JWT_SECRET unset →
  random per-boot secret + loud warning (tokens die on restart).
- SSO/OIDC (implemented, OFF): discovery + JWKS ID-token verify (iss/aud/exp/nonce), PKCE state
  store, SSO_ALLOWED_EMAIL_DOMAINS allowlist, find-or-create by verified email, callback delivers
  tokens ONLY in URL fragment. SAML phase 2 not built.
- Authz: per-route Depends (get_current_user_required/optional, require_admin); AUTHZ_PROVIDER=
  "rbac" (RbacAuthorizationProvider, PermissiveAuthorizer fallback); tenancy BY CONVENTION
  (org_id/app_id columns defaulted 'default-org'/'default-app', _apply_tenant/_apply_owner in the
  entity router); system agents bypass owner filter.
- Tool security: SSRF/path/redact/truncate/injection guards (tool_security.py); path_security_tool;
  sandbox: --network none + cap-drop ALL + Unix-socket LLM proxy (only egress) + docker.sock only
  in sandbox-worker; osv_check (dependency CVEs); tirith_security_tool; skill provenance +
  warn-only SKILL_SCAN_ENABLED=True.

## 13. End-to-End Workflows

1. **Chat turn (v3):** user message → ChatSession + ChatMessage persist → agent resolution →
  createAgentConversation → v3 stream → backend turn_planner forces tool choice → guardrails +
  iteration budget → tools (registry) → fallback generator guarantees artifact/data summary →
  typed SSE events → LiveActivityStream/PlanChecklist → partial content persisted → final message.
2. **User creates an agent:** AgentBuilder chat (agent_builder system meta-agent) → 5-layer
  constitution + skills + KB binding + topology → preflight dry-run → AgentApp row → appears in
  My Space; runtime never bypasses harness assembly (get_system_prompt → registry → memory).
3. **Database analysis:** agent queries bound datasource → Datasource Gateway → DataSnapshot →
  NL2SQL (semantic catalog + schema linking + sqlglot validation) → results summarized w/ sources.
4. **Dashboard generation:** user asks for a dashboard → intent/capability routing → schema
  describe (≤2/turn) → data exploration (≤8/turn) → create_fullstack_dashboard executes IN-PROCESS
  (DashboardAppGenerator) → HTML artifact preview (canvas editor).
5. **PPT deck:** user asks for a deck → deck pipeline (planner/router, data grounding, design
  renderer, audit/blocking pair) → PPTX artifact → inline preview (LibreOffice) — benchmarked
  pro-grade vs Claude/Kimi/ChatGPT.
6. **Workflow (n8n parity):** user draws nodes on canvas or chats Build-with-AI (workflow_builder)
  → save_flow → validate → publish → run (RunDrawer, SSE event log); Code node executes in Docker
  sandbox; Merge node converges parallel branches; db_query NL2SQL-first.
7. **Automation:** user schedules a task (/automation) → automation_agent / automation_runtime
  hidden executor on cron → gateway (email today) → results + artifacts.

## 14. Domain Subsystems

- **Forecasting/BI (Ecisco):** ~60 flags; XGBoost/statsmodels/VAR/foundation models
  (Chronos/Moirai); champion/challenger + shadow runs (migration 074); thresholds MAPE 8/15/25;
  nightly cron 2am; HITL/accuracy feedback; demand signal; ~20 forecasting tables.
- **NL2SQL + semantic layer:** semantic catalog, schema linking/graph + validator + did-you-mean,
  catalog join edges, query cache, NL2SQL shadow mode, response cache; DB-agnostic HARD RULE.
- **Knowledge/RAG:** KnowledgeBase + file datasources (CSV/XLSX upload → queryable sqlite —
  newest), Chroma chat-upload RAG (top_k 8, inline 6000 chars), project knowledge cache,
  embeddings (MEMORY_EMBEDDINGS_ENABLED=True).
- **CAD (Fusion 360):** ~48 tools / 10 modules driving Fusion via socket (host.docker.internal:9876);
  Tier-1 spec: params+contract+closed sketches (docs/superpowers/specs/2026-08-28-cad-agent-tier1*).
- **Artifacts engine:** fullstack dashboards (DashboardAppGenerator), office docs (docx/pptx/xlsx),
  HTML/markdown, canvas editor, provenance/guard tooling.
- **Marketplace & skills:** marketplace sources/agents, skill studio (factory + review queue +
  executions), personal-skills durability volume (zhanlu_user_skills).

## 15. Testing & QA

- Backend: ~904 test files / ~154K LOC — suites: test_forecasting (56), dashboard_app (22),
  knowledge_graph (16), artifacts (15), universal_analytics (11), rag (8), enterprise_orchestrator
  (8), nl2sql (8), tool_handlers (7), document_ingestion (7), + 683 in tests/ root. Newest:
  test_a2a_protocol (6), test_mcp_client_transports (6), test_tracing_spans (8),
  test_computer_use_agent_browser (13), test_swarm_pool (9), test_sso_oidc (26).
- Frontend: 135 colocated Vitest files; 54 source-contract regex pins; key suites: Chat lifecycle,
  LiveActivityStream plan-checklist, WorkflowCanvas drop seeding (NL-first regression),
  MessageBubble web sources, ProjectDetail IA, AgentBuilder ?prefill pin, URL context leak suites.
- Discipline on the RAM-limited Mac: targeted pytest file-only runs; kill stray vitest instances,
  run one, read its log. Roadmap constraint: every feature ships flag-OFF + its own test file;
  tests must pass OFF and ON.

## 16. Docs Archive & Feature Timeline

- 227 .md: numbered 00_read_first (4) → 01_api_contract, 02_database, 03_runtime_contracts,
  04_sandbox_artifacts, 05_mcp_agents_skills, 06_mvp_testing, 07_existing_architecture (13),
  08_latest_ui_runtime_datasource_specs, 09_final_ui_decisions; docs/plans (41 date-stamped),
  docs/superpowers/{plans 67, specs 65, followups, notes, e2e}; root specs + gap.md archives.
- **Feature timeline Jul 28 → Sep 2** (spec filenames): automation chat drawer/deliverables →
  skill-agent UX → dashboard interactivity/dynamic/pro-UI → skill marketplace → forecasting wave
  (true-answers, nightly, analyst, midstream board, mlops-hitl, what-if) → hierarchical LLM config
  → project knowledge graph → llm routing → professional PPT pipeline → business semantic layer →
  schema-aware NL2SQL → dashboard pipeline redesign → goal contract → Aug 25-29: live activity
  stream, qwen3 local perf, activity feed modernization, high-quality PPT, CAD Tier-1, data-driven
  dashboards, CEO CC v1 → **Sep 1-2: workflow builder wave** (agent-workflow-designer,
  workflow-items-pipeline, builder chat, merge/canvas ops, NL2SQL-first db node, sandboxed code
  node, session recognition, swarm fanout, computer use, SSO/OIDC) → modern-agent-gap-roadmap
  (A2A/MCP2026/OTel/swarm-pool/SSO ✅) → 09-02 agent-runtime-vision-voice-design (NEWEST spec,
  commit fd6cf7c, pending approval).
- **modern-agent-gap-roadmap status:** P0 items 1-3 (A2A, MCP 2026, OTel) ✅ IMPLEMENTED;
  P1: computer use facade ✅, swarm pool ✅, SSO/OIDC ✅; voice/realtime + notification channels
  NOT yet; SAML/A2A-SSE/swarm-300 scale = phase 2.

## 17. Known Issues & Gotchas

1. **Bind-mount ≠ hot reload**: UVICORN_RELOAD=false; every backend change = restart BOTH
   zhanlu-backend + zhanlu-sandbox-worker. backend.Dockerfile:129 comment ("auto-reloaded") is stale.
2. **Two schema systems**: alembic 84 + boot-time auto-migrations (main.py) — read both;
   documented drift (agent_memories.project_id).
3. **agents.py monolith** (18,311 LOC) is the runtime; lazy imports dodge circulars; refactor target.
4. **Import-time side-effect registration**: ~140 tools register on import; ordering bugs were real
   (load_skill_body silent miss; Skill meta-tool double-registered as backstop).
5. **Implemented-but-dark is the norm**: FSM, harness, SSO, computer-use, a2a, deep-research-v2,
   otel — all real code, OFF in .env. SYNEXIA_FSM_ENABLED=false means the flagship 12-state FSM
   does NOT run today.
6. **Vision broken with default model** (deepseek-chat text-only; vision_tool posts LLM_MODEL) +
   **voice-out dead code** (useTTS zero importers) — P0 pair spec'd, unbuilt.
7. **JSON columns round-trip as strings** — coerce at boundaries (coerceStringArray); booleans in
   auto-migrations need DEFAULT false on PG.
8. **Tenancy by convention** — org_id/app_id default 'default-org'/'default-app'; system agents
   bypass owner filters; AUTHZ_PROVIDER can drop to permissive.
9. **Frontend duplications**: two ChatInput mounts in Chat.jsx; SSE readers per consumer; mobile
   chat is a parallel stream loop; source-contract tests break on renames/reformatting.
10. **URL params are the conversation state bus** — replaceState sync; any change breaks
    reload/share/steer; heavily pinned by tests.
11. **Infra**: MinIO decorative (postgres_bytea); TWO compose override files disagree (root wins);
    stale /app/venv hijack scar (PYTHONPATH="" on worker); secret sprawl (.env plaintext DB pw,
    truncated keys, admin123 bootstrap); named sandbox_tmp volume unused.
12. **Dark JWT_SECRET**: unset → random per boot → all sessions die on restart (dev surprise).

## 18. How to Run

```bash
cd /Users/minhazulislam/Desktop/zhanlu_sep_1_n8n
make up            # builds 4 sandbox images, then full stack
# Services: backend :5002, frontend :8088 (nginx, dist bind-mount), postgres :5400,
# redis :6300, minio :9400 (console :9401), sandbox-worker (docker.sock)
make logs          # tail compose logs
make smoke         # scripts/smoke_e2e.sh   |  make smoke-ui  # Playwright
make db-setup / db-reset / db-info   # via docker compose run db_manage
# Frontend change → npm run build in frontend/ (dist is :ro)
# Backend change → docker restart zhanlu-backend zhanlu-sandbox-worker
# Tests (RAM-tight): targeted only — python -m pytest backend/tests/<one_file>.py
# Frontend tests: kill stray vitest, run one colocated suite, read its log file.
```
Login: seed admin `admin@zhanlu.dev` / `admin123` (local stack).
