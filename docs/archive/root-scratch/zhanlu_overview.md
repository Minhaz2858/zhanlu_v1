# Zhanlu — Comprehensive Technical Overview

> **Perspective**: AI Engineer  
> **Date**: 2026-09-03  
> **Codebase**: `zhanlu_sep_1_n8n` — full-stack AI agent platform

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [System Architecture](#2-system-architecture)
3. [Setup & Deployment](#3-setup--deployment)
4. [Backend Architecture](#4-backend-architecture)
5. [Frontend Architecture](#5-frontend-architecture)
6. [Agent Loop & AI Model Integration](#6-agent-loop--ai-model-integration)
7. [Synexia FSM — Cognitive Core](#7-synexia-fsm--cognitive-core)
8. [Tool System](#8-tool-system)
9. [Data Flow — End-to-End](#9-data-flow--end-to-end)
10. [Database Models & Schema](#10-database-models--schema)
11. [Context Management & Compaction](#11-context-management--compaction)
12. [Knowledge Base & RAG Pipeline](#12-knowledge-base--rag-pipeline)
13. [Skills System](#13-skills-system)
14. [Dashboard Generation Pipeline](#14-dashboard-generation-pipeline)
15. [Workflow Engine](#15-workflow-engine)
16. [Automation & Scheduling](#16-automation--scheduling)
17. [Authentication & Multi-Tenancy](#17-authentication--multi-tenancy)
18. [API Reference](#18-api-reference)
19. [Configuration & Feature Flags](#19-configuration--feature-flags)
20. [Observability & Tracing](#20-observability--tracing)
21. [Testing Strategy](#21-testing-strategy)
22. [Key Architectural Decisions & Gotchas](#22-key-architectural-decisions--gotchas)
23. [Prompt Engineering Patterns](#23-prompt-engineering-patterns)
24. [Agent Harness — Orchestrator & Run Service](#24-agent-harness--orchestrator--run-service)
25. [LLM Cost Model & Token Budget](#25-llm-cost-model--token-budget)
26. [Sandbox Isolation Model](#26-sandbox-isolation-model)
27. [Adding a New Tool / Skill — Step-by-Step](#27-adding-a-new-tool--skill--step-by-step)
28. [Error Propagation Map](#28-error-propagation-map)
29. [Production Runbook](#29-production-runbook)
30. [Database Migration Workflow](#30-database-migration-workflow)
31. [A2A Protocol & Swarm Architecture](#31-a2a-protocol--swarm-architecture)
32. [Frontend Chat State Machine](#32-frontend-chat-state-machine)
33. [GATE Security Model](#33-gate-security-model)
34. [Persistent Memory & Learning Graph](#34-persistent-memory--learning-graph)
35. [Artifact Generation Pipeline](#35-artifact-generation-pipeline)
36. [SSE/Stream Debugging Guide](#36-ssestream-debugging-guide)
37. [File Upload & Processing Pipeline](#37-file-upload--processing-pipeline)
38. [Performance SLOs & Bottlenecks](#38-performance-slos--bottlenecks)
39. [Architecture Decision Records](#39-architecture-decision-records)
40. [Log Format & Tracing Reference](#40-log-format--tracing-reference)
41. [Contributing Guide](#41-contributing-guide)

**Appendices:**
- [A: Directory Structure](#appendix-a-directory-structure)
- [B: Technology Stack Summary](#appendix-b-technology-stack-summary)
- [C: Deep-Dive Technical Reference](#appendix-c-deep-dive-technical-reference) ← **AI Engineer Implementation Details**
  - C.1 Agent Loop — Exact Execution Flow
  - C.2 Synexia FSM — State-by-State Implementation
  - C.3 Tool Execution Pipeline — Complete Call Stack
  - C.4 LLM Integration — Exact Request Pipeline
  - C.5 Data Agent Sub-Loop — Exact Protocol
  - C.6 Context Compaction — Layer-by-Layer Algorithm
  - C.7 SSE Streaming Protocol — Exact Event Sequence
  - C.8 RAG Pipeline — Exact Retrieval Flow
  - C.9 Confidence Scoring — Exact Formula
  - C.10 Tool Registry — Registration and Dispatch
  - C.11 File Intent Detection — Exact Algorithm
  - C.12 Embedding System — Exact Pipeline
  - C.13 Message Sanitization — Defense-in-Depth
  - C.14 LLM Placeholder Detection
  - C.15 Answer Verification Gate — Exact Checks
  - C.16 Agent Harness — Orchestrator Execution Flow
  - C.17 LLM Request Pipeline — Cost-Aware Model Selection
  - C.18 Sandbox Container Lifecycle — Exact Sequence
  - C.19 Chat.jsx State Transitions — Exact Event Mapping
  - C.20 GATE Policy Evaluation — Decision Tree

---

## 1. Project Overview

**Zhanlu** is a full-stack AI agent platform that enables organizations to create, configure, and interact with LLM-powered agents through a rich web UI. It provides:

- **Conversational AI** — Chat-based agent interaction with streaming SSE responses, tool calling, and artifact generation
- **Multi-Agent Architecture** — Agent Builder for creating specialized agents, sub-agent delegation, and swarm coordination
- **Data Intelligence** — Connect to MySQL/PostgreSQL/SQL Server databases, run NL2SQL queries, and auto-generate dashboards
- **Skill System** — 60+ marketplace skills (PPT generation, research reports, web scraping, document processing, CAD, forecasting)
- **Automation** — Scheduled tasks (cron-based) that run agents unattended with email notification
- **Workflow Engine** — Visual DAG-based workflow builder (n8n-style) with conditional routing
- **Document Generation** — PPT, DOCX, HTML artifacts with layout engines and chart rendering
- **Enterprise RBAC** — Multi-tenant isolation (org_id + app_id), resource sharing, access policies

The platform is designed for the Acme BI domain (C5/C9 industrials) but is generalized for any structured-data business intelligence use case.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Docker Compose Stack                          │
│                                                                         │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐   │
│  │  Frontend     │   │  Backend     │   │  Sandbox Worker          │   │
│  │  (Nginx)     │   │  (FastAPI)   │   │  (Docker-in-Docker)      │   │
│  │  :8088       │──▶│  :5002       │   │  polls Redis queue        │   │
│  │  React/Vite  │   │  Agent FSM   │   │  spawns sandbox containers│   │
│  └──────────────┘   │  Tool exec   │   └──────────────────────────┘   │
│                      │  SSE stream  │                                   │
│                      └──────┬───────┘                                   │
│                             │                                           │
│            ┌────────────────┼────────────────┐                          │
│            ▼                ▼                ▼                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                 │
│  │  PostgreSQL   │  │  Redis       │  │  MinIO       │                 │
│  │  :5400       │  │  :6300       │  │  :9400/9401  │                 │
│  │  Primary DB  │  │  Job queue   │  │  Blob storage│                 │
│  └──────────────┘  │  Caching     │  └──────────────┘                 │
│                    │  Events      │                                    │
│                    └──────────────┘                                    │
└─────────────────────────────────────────────────────────────────────────┘

External integrations:
  ┌─────────────────┐  ┌─────────────┐  ┌──────────────────┐
  │  LLM Provider   │  │  MySQL DW   │  │  Search Provider │
  │  DeepSeek/API   │  │  Read-only  │  │  Tavily/Serper   │
  │  OpenAI-compat  │  │  mirror     │  │  DuckDuckGo      │
  └─────────────────┘  └─────────────┘  └──────────────────┘
```

### Key Networks
- **app_net** — backend ↔ sandbox-worker (internal service communication)
- **data_net** — backend / postgres / redis / minio (data layer isolation)

---

## 3. Setup & Deployment

### Prerequisites
- Docker + Docker Compose
- `.env` file configured (copy from `.env.example`)

### Quick Start
```bash
make up              # Build + start full stack
make logs            # Tail all service logs
make db-info         # Verify database connectivity
make smoke           # Run E2E smoke test
```

### Development
```bash
# Frontend dev (Vite proxies /api → localhost:5002)
cd frontend && npm run dev    # → :5157

# Backend health
curl localhost:5002/healthz
```

### Deployment Hard Rule
After ANY backend code change, restart **both** containers:
```bash
docker restart zhanlu-backend zhanlu-sandbox-worker
```
Bind-mounts serve updated files but never reload a running process's imported modules.

### Sandbox Images
Pre-built Docker images for isolated code execution:
- `zhanlu-sandbox-python` — Python code execution
- `zhanlu-sandbox-office` — DOCX/XLSX generation
- `zhanlu-sandbox-pptx` — PPT generation with LibreOffice
- `zhanlu-sandbox-webapp` — Web app building

---

## 4. Backend Architecture

### Entry Point: `backend/main.py`

The FastAPI application is created via `create_app()` with a structured `lifespan` context manager that handles:

1. **DB Schema** — `Base.metadata.create_all()` + auto-migrations (SQLite PRAGMA or Postgres `ALTER TABLE ADD COLUMN IF NOT EXISTS`)
2. **Hook Loading** — DB-backed lifecycle hook rules
3. **Marketplace Sync** — Seed curated skill sources in background thread
4. **System Agent Seeding** — `ensure_system_agents()` for agent_builder, skill_agent, etc.
5. **Super-admin Bootstrap** — `ensure_superadmin()` from env vars
6. **LLM Catalog Seeding** — Well-known models (kimi-k2, MiniMax-M2, etc.)
7. **Prompt Normalization** — Normalize `prompt_tools` for agents with bound KBs
8. **Service Initialization** — Agent definitions, skills registry, tool output manager
9. **Scheduled Tasks** — Memory consolidation, skill curation background loop
10. **Automation Dispatcher** — Manus-style execution engine
11. **Workflow Scheduler** — n8n Schedule Trigger daemon
12. **Catalog Watchdog** — Recovers from stuck indexing status
13. **Dashboard App Manager** — Mounts persisted full-stack dashboard apps
14. **FastMCP Transport** — External MCP client endpoint

### Router Registration (38 routers)

All routers are registered under `/api` prefix:

| Router | Purpose |
|--------|---------|
| `agents` | Agent conversations, streaming, v2/v3 message handling (~12.4k lines) |
| `agent_studio` | Agent CRUD, prompt layers, data/skill bindings |
| `auth` / `auth_sso` | JWT auth, SSO/OIDC, Feishu QR login |
| `knowledge_bases` | KB CRUD, reingest, semantic catalog |
| `dashboards` | Dashboard CRUD, live data polling |
| `workflows` | Workflow DAG CRUD, execution |
| `automation_api` | Scheduled task CRUD, execution history |
| `artifacts` | PPT/DOCX/HTML artifact download |
| `sandbox` | Code execution job submission |
| `nl2sql` | Natural language to SQL conversion |
| `marketplace` | Skill/agent marketplace browsing |
| `skill_studio` | Skill CRUD, dry-run, provenance |
| `mcp` | MCP server/client management |
| `a2a` | Agent-to-Agent protocol (Google A2A) |
| `project_catalog` | Data map, catalog overlays |
| `project_memories` | Project-scoped memory review |
| `admin_*` | User management, evals, observability |
| `entities` | Generic entity CRUD (auto-generated per model) |

### Key Modules (`backend/app/`)

```
app/
├── config.py          # Pydantic Settings — 1500+ lines, 200+ env vars
├── database.py        # SQLAlchemy engine, SessionLocal, Redis
├── deps.py            # FastAPI dependencies (get_db, get_current_user)
├── models/            # 85 SQLAlchemy models
├── routers/           # 40 FastAPI routers
├── schemas/           # Pydantic request/response schemas
├── services/          # 100+ service modules (core business logic)
├── utils/             # Utility functions
├── middleware/         # Auth, rate limiting middleware
└── core/              # Core utilities
```

---

## 5. Frontend Architecture

### Stack
- **React 18** + **Vite 6** (JSX, not TypeScript)
- **Tailwind CSS** + **Radix UI** primitives (shadcn/ui pattern)
- **@base44/sdk** — Data layer (CRUD client, auth)
- **react-router-dom** v6 — Routing
- **@tanstack/react-query** — Server state
- **recharts** — Charting
- **lucide-react** — Icons
- **react-resizable-panels** — Layout
- **i18n** — `translations.js` with `t.<section>.<key>` access pattern

### Route Structure

```
/ (root)              → Chat (primary view)
/chat                 → Chat
/automation           → AutomationTasks
/automation/chat      → Chat (automation mode)
/automation/:id       → AutomationTaskDetail
/my-space             → MySpace (projects, agents, workflows)
/my-space/project/:id → ProjectDetail
/my-space/agent/:id   → AgentConfig
/market               → Market (skill/agent marketplace)
/toolkit              → Toolkit
/agent-builder        → AgentBuilder
/settings             → Settings
/admin/users          → AdminUsers
/admin/observability  → AdminObservability
/dashboard/:id        → DashboardView
/my-files             → MyFilesPage
```

Mobile detection (width ≤ 1024 + touch) switches to a separate lightweight route tree (`MobileLayout` + bottom tabs).

### Key Frontend Modules

| Path | Purpose |
|------|---------|
| `src/api/agentEnhanced.js` | SSE streaming, permission mode, chat search |
| `src/api/base44Client.js` | @base44/sdk wrapper, token injection |
| `src/api/tokenRefresh.js` | JWT rotation, single-flight refresh |
| `src/api/authFetch.js` | Authenticated fetch with 401 recovery |
| `src/lib/AuthContext.jsx` | Auth state, proactive token refresh (~80% TTL) |
| `src/lib/ChatSessionContext.jsx` | Session management, agent binding |
| `src/lib/PersistentStreamContext.jsx` | SSE stream lifecycle |
| `src/lib/translations.js` | i18n (en/zh) |
| `src/pages/Chat.jsx` | Main chat page — message handling, SSE, file uploads |
| `src/components/chat/` | 94 files — message bubbles, activity stream, input bar |
| `src/components/dashboard/` | 31 files — live chart rendering, design controls |
| `src/components/workflow/` | 11 files — @xyflow/react visual DAG editor |

### SSE Streaming Protocol (v3)

The frontend connects to `POST /api/apps/{appId}/agents/conversations/v3/{id}/messages/stream` which returns Server-Sent Events:

| Event | Payload | Purpose |
|-------|---------|---------|
| `delta` | `{content}` | Streaming text chunks |
| `tool_call` | `{name, args, call_id}` | Tool invocation notification |
| `tool_result` | `{call_id, result}` | Tool execution result |
| `activity_step` | `{step, label, status}` | Progress indicator |
| `phase` | `{headline, plan}` | Current cognitive phase |
| `artifact` | `{id, type, title}` | Generated artifact notification |
| `live_event` | `{type, label_key, params}` | Structured activity feed |
| `content_preserve` | `{content}` | Pre-replace content snapshot |
| `content_replace` | `{content}` | Full content replacement (refinement) |
| `sources` | `{entries}` | Data source citations |
| `done` | `{}` | Stream complete |

---

## 6. Agent Loop & AI Model Integration

### Architecture Overview

The agent loop is the heart of Zhanlu — it orchestrates LLM calls, tool execution, and streaming responses. There are three parallel loop implementations in `agents.py`:

1. **v2 Loop** (~line 3469) — Legacy request/response mode
2. **Resume Loop** (~line 4791) — Checkpoint/resume for long-running tasks
3. **v3 SSE Loop** (`_stream_llm_with_tools`, ~line 7328) — Primary streaming path

### v3 Streaming Flow

```
User Message
    │
    ▼
POST /v3/.../messages/stream
    │
    ▼
add_message_stream()                    ← agents.py:3248
    │
    ├─ prepare_data_source_runtime()    ← Build data context, bound_kb_ids
    ├─ _compute_tool_choice()           ← Force ask_data_agent on iter 0 if data question
    │
    ▼
ReAct Loop (max MAX_TOOL_ITERATIONS=40, TOOL_CALL_HARD_CAP=10)
    │
    ├─ Call LLM with tools + system prompt
    │   ├─ stream_chat_completion()     ← llm_service.py
    │   │   ├─ Provider health check    ← provider_health.py
    │   │   ├─ Fallback chain           ← LLM_FALLBACK_PROVIDERS
    │   │   └─ Parallel tool calls      ← LLM_PARALLEL_TOOL_CALLS_ENABLED
    │   │
    │   ├─ Emit SSE events (delta, tool_call, phase, live_event)
    │   │
    │   └─ If tool_calls in response:
    │       ├─ _run_tool_batch()        ← agents.py:11223
    │       │   ├─ execute_tool_with_retry()  ← agent_tools.py
    │       │   │   ├─ Result-level retry (TOOL_RETRY_MAX_ATTEMPTS=2)
    │       │   │   └─ Arg reformulation (TOOL_REFORMULATE_MAX_ATTEMPTS=1)
    │       │   └─ Tool loop guardrails ← tool_loop_guardrails.py
    │       │
    │       ├─ Emit tool_result SSE
    │       └─ Feed results back to LLM (next iteration)
    │
    ├─ Finish-line forcing (tool_choice="none" at T-3)
    ├─ Self-evaluation gate             ← answer_verification.py
    │   ├─ Deterministic checks (metadata-only, empty, degenerate)
    │   └─ Optional LLM rubric (4-check: completeness, quality, source, plausibility)
    │
    ├─ synthesize_report()              ← Final synthesis LLM call
    └─ finalize_into_artifact()         ← Persist PPT/DOCX artifacts
```

### LLM Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Primary LLM endpoint |
| `LLM_MODEL` | `gpt-4o-mini` | Default model name |
| `LLM_FALLBACK_PROVIDERS` | `""` | JSON array of backup providers |
| `LLM_FIXED_TEMPERATURE_MODELS` | `kimi-k2,o1,o3,qwen3,deepseek-v4` | Models that reject temperature |
| `LLM_MAX_TOKENS_HARD_CAP` | 4096 | Global max_tokens safety net |
| `LLM_PARALLEL_TOOL_CALLS_ENABLED` | `false` | Enable parallel tool calls |
| `LLM_HEALTH_ROUTING_ENABLED` | `false` | Circuit-breaker provider selection |
| `MODEL_TASK_ROUTING` | `""` | Task-type → model mapping |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Semantic embeddings |

### Provider Health & Fallback

The system implements a circuit-breaker pattern:
- `provider_health.py` tracks success/failure per provider
- When the primary fails (HTTPStatusError, RequestError), `call_llm` falls through to `LLM_FALLBACK_PROVIDERS`
- `select_provider()` can route to the healthiest provider when `LLM_HEALTH_ROUTING_ENABLED=True`

---

## 7. Synexia FSM — Cognitive Core

The **Synexia Finite State Machine** (`backend/app/services/synexia/`) is the cognitive architecture that replaces the raw tool loop. Enabled by `SYNEXIA_FSM_ENABLED=True` (default).

### States & Transitions

```
ORIENT ──▶ PLAN ──▶ ACT ──▶ VERIFY ──▶ SYNTHESIZE ──▶ FINALIZE
  │          │        │        │
  │          │        │        └─▶ (re-plan if SYNEXIA_VERIFY_REPLAN_MAX > 0)
  │          │        │
  │          │        └─▶ (re-plan if SYNEXIA_ACT_REPLAN_MAX > 0)
  │          │
  └──────────┘  (re-orient on ambiguity)
```

### Key FSM Modules

| Module | Purpose |
|--------|---------|
| `fsm.py` | State machine transitions, phase entry/exit hooks |
| `capability_router.py` | Routes user intent to tool/skill/data pipeline |
| `context_assembler.py` | Builds the context window for each LLM call |
| `plan_dag.py` | Directed Acyclic Graph for plan node dependencies |
| `adaptive_planner.py` | Adjusts plan based on intermediate results |
| `intent_router.py` | Classifies user intent (data, research, creative, code) |
| `data_intent.py` | Detects database query intent |
| `verifier.py` | Deterministic + LLM verification of answers |
| `confidence_scorer.py` | Artifact confidence scoring |
| `quality_eval.py` | Quality gate for shipped artifacts |
| `grounding_extractor.py` | Citation grounding from tool results |
| `finalize.py` | Artifact shipping with quality threshold |
| `policy_evaluator.py` | GATE policy evaluation |
| `reflexion.py` | Self-reflection on plan execution |
| `smart_retry.py` | Intelligent retry with alternative approaches |
| `user_signal.py` | Detects user frustration / clarification needs |
| `report_synthesis.py` | Multi-section report building |

### Self-Evaluation Stack

```
SYNEXIA_VERIFY_REPLAN_MAX = 1      # Max VERIFY → PLAN loops
SELF_EVAL_MAX_REPLANS = 1          # Total re-plan attempts
GOAL_CONTRACT_MAX_FORCES = 2       # Max forced nudge attempts
SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE = 200  # Chars for data-sufficient fast-path
```

The data-sufficient fast-path skips the verification gate after 1 nudge when the agent has data + ≥200 chars of prose.

---

## 8. Tool System

### Tool Registry (`tool_registry.py`)

Singleton `ToolRegistry` maps tool names → `ToolEntry` objects containing:
- `schema` — OpenAI function-calling format
- `handler` — Sync or async callable `(args, db, user_id, **ctx) → dict`
- `category` — Grouping tag
- `toolset` — Hermes-style toolset grouping
- `enabled_by_default` — Whether the tool appears without explicit opt-in
- `requires_env` — Required environment variables
- `check_fn` — Runtime availability check
- `max_result_size_chars` — Per-tool output cap

### Dynamic Tool Loading

When `TOOL_LOADING_MODE="dynamic"` (default), each turn:
1. Keeps always-on core tools (memory, web_search, create_artifact, etc.)
2. Adds intent-relevant periphery tools (top-K by embedding/lexical match)
3. Falls back to full list on any selection error

Core tools (`TOOL_LOADING_CORE`): memory, project_memory, session_search, clarify, read_file, write_file, execute_code, sandbox_code, web_search, web_extract, search_skills, list_skills, list_knowledge_bases, delegate_task, send_message, interrupt, sql_query, ask_data_agent, comprehensive_data, create_dashboard, create_fullstack_dashboard, edit_dashboard, create_artifact, edit_artifact, load_skill_body, skill_manager, approval

### Tool Categories (100+ tools in `tool_handlers/`)

| Category | Examples |
|----------|----------|
| **Data** | `sql_query`, `ask_data_agent`, `comprehensive_data`, `describe_schema`, `execute_query`, `list_data_sources` |
| **Dashboards** | `create_dashboard`, `create_fullstack_dashboard`, `edit_dashboard`, `list_fullstack_dashboards` |
| **Artifacts** | `create_artifact`, `edit_artifact`, `create_presentation`, `deck_edit_*` (6 edit tools) |
| **Memory** | `memory`, `project_memory`, `session_search`, `todo` |
| **Files** | `read_file`, `write_file`, `list_files`, `upload_file` |
| **Code** | `execute_code`, `sandbox_code`, `code_execution` |
| **Web** | `web_search`, `web_extract`, `url_safety_check`, `agent_browser` |
| **Delegation** | `delegate_task`, `send_message`, `interrupt`, `clarify`, `approval` |
| **Skills** | `skill_manager`, `load_skill_body`, `skills_hub`, `search_skills` |
| **MCP** | `mcp_call_tool`, `mcp_proxy` |
| **Forecasting** | `forecast`, `backtest` |
| **CAD** | `fusion360_*` (10+ tools) |
| **Communication** | `feishu_doc`, `feishu_drive`, `microsoft_graph`, `discord` |
| **Vision** | `vision_analyze`, `image_generation`, `video_generation`, `transcription` |
| **Swarm** | `swarm_spawn`, `swarm_send`, `swarm_broadcast` |

### Tool Execution Pipeline

```
LLM emits tool_calls
    │
    ▼
_run_tool_batch()
    │
    ├─ Tool loop guardrails check (per-tool caps, total iteration budget)
    ├─ execute_tool_with_retry()
    │   ├─ Registry dispatch → handler(args, db, user_id, **ctx)
    │   ├─ On failure: result-level retry (TOOL_RETRY_MAX_ATTEMPTS=2)
    │   ├─ On persistent failure: arg reformulation (TOOL_REFORMULATE_MAX_ATTEMPTS=1)
    │   │   └─ LLM suggests corrected arguments → retry once
    │   └─ Tool result persistence → tool_result_storage
    │
    └─ Tool output truncation (TOOL_MAX_OUTPUT_CHARS=8000, per-model caps)
```

### Per-Tool Caps (Hard Limits)

```python
memory:1, ask_data_agent:2, interrupt:2, clarify:3
```

---

## 9. Data Flow — End-to-End

### User Query to Answer (Data Question)

```
1. User types "Show me C5 sales by region for Q3"
2. Frontend → POST /v3/.../messages/stream (SSE)
3. Backend: add_message_stream()
   ├─ detect data intent → force ask_data_agent via tool_choice
   ├─ prepare_data_source_runtime() → bind KBs
   └─ Enter ReAct loop
4. LLM calls ask_data_agent(question="Show me C5 sales...", data_source_id=...)
5. _ask_data_agent sub-loop (max 4 iterations):
   ├─ Sub-LLM picks internal tool: list_data_sources → describe_schema → execute_query
   ├─ SQL generated (NL2SQL or LLM) against bound MySQL/PG data source
   ├─ Results returned as rows
   └─ answer_from_database synthesizes natural language answer
6. Outer loop receives sub-agent result
7. synthesize_report() → final answer with citations
8. SSE events stream to frontend:
   ├─ delta (text chunks)
   ├─ tool_call / tool_result (activity indicators)
   ├─ sources (data source citations)
   └─ done (completion signal)
9. Frontend renders:
   ├─ MessageBubble with markdown + source chips
   ├─ Activity feed with tool call history
   └─ Optional artifact (PPT, dashboard, chart)
```

### Data Source Binding Flow

```
AgentApp.data_bindings → [kb_id_1, kb_id_2]
    │
    ▼
prepare_data_source_runtime()
    ├─ Resolve KBs → connection details (host, port, db, schema)
    ├─ Build data_ctx_extras:
    │   ├─ bound_kb_ids: [kb_id_1, kb_id_2]
    │   ├─ schema_descriptions: per-KB table/column metadata
    │   └─ concept_catalog: business term → table.column mappings
    └─ Inject into system prompt as "Bound Data Sources" block
```

---

## 10. Database Models & Schema

### 85 SQLAlchemy Models in `backend/app/models/`

### Core Models

| Model | Table | Purpose |
|-------|-------|---------|
| `User` | `users` | Auth, roles, profile |
| `Organization` | `organizations` | Multi-tenant org |
| `AppWorkspace` | `app_workspaces` | Tenant app context |
| `Project` | `projects` | Project grouping |
| `AgentApp` | `agent_apps` | Agent configuration (30+ fields) |
| `AgentConversation` | `agent_conversations` | Chat sessions |
| `ChatMessage` | `chat_messages` | Messages with rich content |
| `ChatSession` | `chat_sessions` | Frontend chat session |
| `KnowledgeBase` | `knowledge_bases` | DB connections, file sources |
| `Tool` | `tools` | Tool metadata |
| `Artifact` | `artifacts` | Generated deliverables (PPT/DOCX/HTML) |
| `AutomationTask` | `automation_tasks` | Scheduled tasks |
| `AutomationExecution` | `automation_executions` | Run history |
| `AgentMemory` | `agent_memories` | Per-agent memory store |
| `AgentRun` / `AgentRunStep` | `agent_runs` / `agent_run_steps` | Agent observability |
| `LlmModel` | `llm_models` | Model catalog |
| `McpServer` | `mcp_servers` | MCP server configs |
| `WorkflowRun` | `workflow_runs` | Workflow execution log |

### AgentApp — The Central Entity

The `AgentApp` model is the most complex with 30+ fields including:

- **Prompt Layers**: `prompt_identity`, `prompt_boundary`, `prompt_reasoning`, `prompt_tools`, `prompt_output`
- **Capabilities**: `capabilities` (JSON), `model`, `agent_type`
- **Bindings**: `skills` (JSON), `knowledge_bases` (JSON), `data_bindings` (JSON), `skill_bindings` (JSON)
- **Topology**: `topology`, `sub_agents` (JSON), `flow_mode`, `flow` (graph JSON)
- **Enterprise**: `manifest_json`, `memory_scope`, `policy_profile`, `output_contract`, `evaluation_profile`
- **Runtime**: `tool_config` (JSON), `progressive_disclosure`, `is_system`, `role`, `resource_type`

### Multi-Tenant Isolation

Every table has `org_id` + `app_id` columns (auto-migrated on startup). The `_apply_tenant` and `_apply_owner` functions enforce row-level isolation.

### Schema Evolution

- **Alembic**: `backend/alembic/` with 85+ migration files
- **Auto-migrations**: `main.py` `_run_auto_migrations()` adds missing columns on startup
- **Specific migrations**: `_specific_migrations()` dict maps table → column additions

---

## 11. Context Management & Compaction

The context window is a scarce resource. Zhanlu implements a 4-layer progressive compaction pipeline.

### Architecture

```
Pluggable Context Engine (app/services/context_engine.py)
    │
    ▼
Default Engine wraps 4-layer pipeline:
    │
    Layer 1: pre_api_prune     — Drop oldest tool-role messages (no LLM)
    Layer 2: microcompact      — Summarize last N tool exchanges (30K token threshold)
    Layer 3: full_compact      — Full conversation summary (50K token threshold)
    Layer 4: session_memory    — Persist key facts to AgentMemory before compaction
```

### Key Parameters

| Setting | Default | Description |
|---------|---------|-------------|
| `COMPACTION_AUTO_THRESHOLD` | 0.8 | Context usage % that triggers compaction |
| `COMPACTION_MICROCOMPACT_THRESHOLD` | 30,000 | Token count for microcompact |
| `COMPACTION_FULL_THRESHOLD` | 50,000 | Token count for full compact |
| `COMPACTION_PRESERVE_RECENT` | 6 | Messages protected from compaction |

### Context Escalation Ladder

When context overflow is imminent, actions are applied in order:
1. `compact` — Run auto_compact_if_needed
2. `truncate_tool_outputs` — Apply per-model tool-output caps
3. `drop_old_tool_messages` — Drop oldest tool-role messages
4. `fallback_to_different_model` — Switch to a model with larger context

### Tool Output Caps by Model

```python
TOOL_OUTPUT_CAP_BY_MODEL = {
    "qwen3.6-27b": 12288,
    "deepseek-v4-flash": 24576,
    "deepseek-chat": 24576,
    "gpt-4o": 16384,
    "gpt-4o-mini": 16384,
    "claude-sonnet": 32768,
    "claude-opus": 32768,
}
```

---

## 12. Knowledge Base & RAG Pipeline

### Knowledge Base Types

| `source_kind` | Description | Indexing |
|---------------|-------------|----------|
| `db` | MySQL / PostgreSQL / SQL Server connection | Semantic catalog (`catalog_status`) |
| `file` | PDF / DOCX / HTML / TXT upload | ChromaDB vector index (`indexing_status`) |

### DB Source Flow

```
1. User creates KB with connection details (host, port, db, credentials)
2. SEMANTIC_CATALOG_ENABLED → catalog_indexer runs:
   ├─ Discover tables + columns
   ├─ Extract FK relationships
   ├─ Build concept catalog (business term → table.column)
   └─ catalog_status: pending → indexing → ready | error
3. Agent runtime: prepare_data_source_runtime()
   ├─ Builds schema descriptions
   └─ Injects into system prompt
4. NL2SQL: user question → SQL query via LLM
5. execute_query: runs SQL against source DB
6. Results returned as rows for synthesis
```

### File Source Flow (RAG)

```
1. User uploads file → stored in uploads/ + MinIO
2. Document ingestion pipeline (app/services/document_ingestion/):
   ├─ Extract text (pypdf for PDF, mammoth for DOCX, BS4 for HTML)
   ├─ Chunk text (sentence-boundary splitting)
   ├─ Embed chunks (sentence-transformers MiniLM, LOCAL — no cloud)
   └─ Store in ChromaDB collection
3. indexing_status: pending → indexing → ready | failed
4. Retrieval: rag_retriever → top-K chunks by cosine similarity
5. Injected into system prompt as context blocks
```

### Chat Upload RAG (2026-08-31)

Large uploaded files (and multi-file turns) are routed through ChromaDB instead of dumping full text into the prompt:
- `RAG_UPLOADS_INLINE_MAX_CHARS = 6000` — Files below this stay inline
- `RAG_UPLOADS_TOP_K = 8` — Top-k relevant chunks per turn
- `RAG_UPLOADS_MAX_FILES_INLINE = 3` — More files → switch to retrieval

---

## 13. Skills System

### Architecture

```
backend/skills/               ← 60+ skill directories
    ├── <skill-name>/
    │   ├── skill.md          ← Skill definition (prompt, tools, metadata)
    │   ├── run.py            ← Optional Python handler
    │   └── ...
    │
skills_loader.py              ← Discovers and registers skills
skill_routing/meta_tool.py    ← Unified "skill" meta-tool
tool_handlers/skills_tool.py  ← Skill execution dispatcher
```

### Skill Categories

| Category | Skills |
|----------|--------|
| **Document Generation** | `weekly-report-generation`, `business-report-generator`, `document-skills`, `ppt_skills` |
| **Research** | `research-paper-writing`, `sciwrite`, `equity-research-report`, `competitor-analysis` |
| **Content** | `better-writing`, `content-creation-publisher`, `anti-slop-writing` |
| **Design** | `canvas-design`, `theme-factory`, `ui-ux-pro-max`, `brand-guidelines` |
| **Data** | `dashboard-generation`, `data-viz-design` |
| **Development** | `agent-skill-creator`, `skill-creator`, `mcp-builder`, `agent-browser` |
| **Integration** | `connect-apps`, `composio-skills` (832 sub-skills) |
| **Specialized** | `fusion360_*` (CAD), `minimax_skills` (vision), `forecast_tool` |

### Skill Loading

1. `get_skills_registry().load()` — Scans `backend/skills/` on startup
2. Each `skill.md` defines: name, description, tools, prompt template
3. Skills are registered in the `ToolRegistry` as callable tools
4. `skill_manager` tool enables CRUD operations on skills
5. Progressive disclosure: only name + description + summary injected; full body loaded on demand via `load_skill_body`

### Skill Execution

- **In-process**: Most skills execute directly in the backend process
- **Sandbox**: Code-heavy skills (Python scripts, web apps) run in isolated Docker containers via `sandbox_worker`
- **Sandbox flow**: Backend enqueues job → Redis `sandbox:queue` → Worker BRPOPs → Spawns container → Result written back

---

## 14. Dashboard Generation Pipeline

### Full-Stack Dashboard Architecture

```
1. User: "Create a sales dashboard"
2. LLM calls create_fullstack_dashboard(config)
3. DashboardAppGenerator (in-process, NOT sandbox):
   ├─ Generate backend API (realtime.py.jinja2 + queries.py)
   │   ├─ SQL queries from config
   │   └─ FastAPI sub-router with SSE polling
   ├─ Generate frontend (App.jsx + useDashboardStream.js)
   │   ├─ React component with recharts
   │   └─ Vite dev server config
   └─ Mount as persisted dashboard app
4. Dashboard served at /dashboard/{id}
5. Live data: SSE polling from generated backend → frontend recharts
```

### Dashboard Persistence

- `dashboard_apps` table stores the generated config
- `dashboard_app_manager.init_app()` mounts all persisted apps on startup
- `dashboard_versions` table tracks version history
- Frontend uses `useDashboardStream.js` hook for live data

---

## 15. Workflow Engine

### n8n-Style Visual Workflow Builder

```
Workflow Canvas (frontend):
  ┌───────┐     ┌───────┐     ┌───────┐
  │Trigger │────▶│  LLM  │────▶│Output │
  └───────┘     └───────┘     └───────┘
                    │
               ┌────┴────┐
               │Condition │
               └────┬────┘
               ┌────┴────┐
          ┌────┴──┐  ┌──┴────┐
          │ Path A │  │ Path B│
          └───────┘  └───────┘
```

### Node Types (`workflow_node_types.py`)

- **Trigger** — Schedule, chat message, webhook
- **LLM** — OpenAI-compatible completion
- **Agent** — Delegate to an AgentApp
- **Condition / Switch** — Branch routing
- **Code** — Python execution
- **Tool** — Call a registered tool
- **Output** — Send result

### Execution (`workflow_executor.py`)

- Topological sort for execution order
- Router nodes return `_route` indicating active output ports
- Context: `ctx.trigger_input`, `ctx.outputs`, `ctx.record_step()`
- Conditional routing: only downstream nodes of active ports execute

---

## 16. Automation & Scheduling

### Architecture

```
AutomationTask (cron expression, agent_id, prompt)
    │
    ▼
automation_dispatcher.py
    ├─ Cron-based scheduling (croniter for next_run_at)
    ├─ Job execution:
    │   ├─ Creates AgentConversation
    │   ├─ Runs agent loop (unattended — no user in loop)
    │   ├─ Synthesizes result
    │   └─ Stores AutomationExecution record
    ├─ Notification gateway (optional email with artifacts)
    └─ Zombie reaper on startup
```

### Key Features
- **LLM-informed tick** (`llm_informed_tick=True`) — Agent decides whether a scheduled run is needed
- **Activity steps** — Manus-style execution progress tracking
- **Timeout + lease** — Prevents stuck executions with `timeout_at` + `lease_owner`
- **Email notification** — SMTP gateway with attachment size limits (8MB → signed download link)

---

## 17. Authentication & Multi-Tenancy

### Auth Flow

```
1. Register: POST /auth/register → OTP email → verify → account created
2. Login: POST /auth/login → JWT access token (15 min) + refresh token (30 days)
3. Token refresh: POST /auth/refresh → rotated refresh token + new access token
4. Logout: POST /auth/logout → revoke access JTI + invalidate all refresh tokens
```

### Token Details
- **Access JWT**: 15 min TTL, carries `jti` for blacklisting
- **Refresh token**: 30 days, one-time-use rotation, stored as SHA-256 hash
- **Proactive refresh**: Frontend refreshes at ~80% TTL + on window focus
- **Single-flight**: `tokenRefresh.js` ensures only one concurrent refresh

### Multi-Tenant Isolation
- Every table has `org_id` + `app_id` (auto-migrated on startup)
- `_apply_tenant(org_id, app_id)` filters queries
- `_apply_owner(user_id)` scopes to user's resources
- `resource_type`: `personal` (user-created) vs `company` (admin-created)
- Resource sharing: `resource_shares` + `resource_access_policies`

### SSO / OIDC
- `SSO_ENABLED` + `SSO_PROVIDERS` (JSON array of provider configs)
- Supported: Google, custom OIDC providers
- Feishu (Lark) QR-code login (`FEISHU_APP_ID`)

---

## 18. API Reference

### Base URL
- Development: `http://localhost:5002/api`
- All endpoints require authentication (except `/healthz`, `/share/c/<token>`)

### Key Endpoint Groups

#### Chat & Agents
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/apps/{app}/agents/conversations` | List conversations |
| `POST` | `/apps/{app}/agents/conversations` | Create conversation |
| `POST` | `/apps/{app}/agents/conversations/v3/{id}/messages/stream` | **Streaming chat** (SSE) |
| `PUT` | `/apps/{app}/agents/conversations/{id}` | Update conversation |
| `GET` | `/apps/{app}/agents/conversations/{id}/permission-mode` | Get permission mode |

#### Agent Studio
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/agents/apps` | List agent apps |
| `POST` | `/api/agents/apps` | Create agent |
| `PUT` | `/api/agents/apps/{id}` | Update agent |
| `POST` | `/api/agents/apps/{id}/data-bindings` | Add data binding |
| `POST` | `/api/agents/apps/{id}/skill-bindings` | Add skill binding |

#### Knowledge Bases
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/knowledge-bases` | List KBs |
| `POST` | `/api/knowledge-bases` | Create KB |
| `POST` | `/api/knowledge-bases/{id}/reingest` | Retry ingestion |
| `POST` | `/api/knowledge-bases/{id}/catalog/index` | Trigger semantic catalog |
| `GET` | `/api/knowledge-bases/{id}/catalog/status` | Catalog indexing status |

#### Dashboards
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dashboards` | List dashboards |
| `POST` | `/api/dashboards` | Create dashboard |
| `GET` | `/api/dashboards/{id}/poll` | Live data poll (SSE) |

#### Automation
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/automations` | List tasks |
| `POST` | `/api/automations` | Create task |
| `GET` | `/api/automations/{id}/executions` | Execution history |

#### Workflows
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/workflows` | List workflows |
| `POST` | `/api/workflows` | Create workflow |
| `POST` | `/api/workflows/{id}/run` | Execute workflow |

#### Artifacts
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/artifacts` | List artifacts |
| `GET` | `/api/artifacts/{id}/download` | Download artifact file |

#### Project Knowledge
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/project-knowledge/{id}/stats` | Project knowledge stats |
| `POST` | `/api/project-knowledge/{id}/reindex` | Trigger reindex |
| `POST` | `/api/project-knowledge/{id}/invalidate` | Clear cache |

#### Admin
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/admin/users` | List users |
| `POST` | `/api/admin/users` | Create user |
| `GET` | `/api/admin/evals/regression` | Golden eval preflight |
| `GET` | `/api/admin/observability/traces` | Agent trace search |

#### A2A (Agent-to-Agent)
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/.well-known/agent.json` | Agent Card (Google A2A spec) |
| `POST` | `/a2a` | JSON-RPC A2A message |

---

## 19. Configuration & Feature Flags

**CRITICAL RULE**: All feature flags default to `False`. Always check with `getattr(settings, "FLAG", False)`, never `os.environ.get()`.

### Flag Categories

| Category | Flags | Default |
|----------|-------|---------|
| **Auth** | `ALLOW_PUBLIC_REGISTRATION`, `ALLOW_SELF_REGISTRATION`, `SSO_ENABLED` | False |
| **LLM** | `LLM_PARALLEL_TOOL_CALLS_ENABLED`, `LLM_HEALTH_ROUTING_ENABLED`, `LLM_RESPONSE_CACHE_ENABLED`, `PROMPT_CACHE_ENABLED` | False |
| **Agent** | `SYNEXIA_FSM_ENABLED`, `SYNEXIA_VERIFIER_LLM_ENABLED`, `TOOL_ARG_REFORMULATION_ENABLED`, `DEEP_RESEARCH_ENABLED` | True/False mixed |
| **Data** | `COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED`, `COMPREHENSIVE_DATA_GATE_ENABLED`, `RAG_UPLOADS_ENABLED` | True/False |
| **Sandbox** | `SANDBOX_BACKEND`, `AUDIO_TRANSCRIBE_ENABLED`, `IMAGE_OCR_ENABLED` | False |
| **Enterprise** | `CHANNELS_ENABLED`, `NOTIFICATION_GATEWAY_ENABLED`, `RATE_LIMIT_ENABLED` | False |
| **Eval** | `EVAL_PIPELINE_ENABLED`, `EVAL_GATE_ENABLED`, `PROMPT_AB_TEST_ENABLED` | False |
| **NLU** | `INTENT_PLANNER_ENABLED`, `QUERY_REWRITE_ENABLED`, `RETRIEVAL_HYBRID_ENABLED` | False |

---

## 20. Observability & Tracing

### Tracing System (`app/services/tracing/`)

- `TraceContext` — Per-request trace ID propagation
- `note_llm_usage()` — Token usage recording
- `llm_completion_span()` — LLM call timing
- `traced_llm_call()` — Full LLM call tracing
- `workflow_node_span()` — Workflow node execution tracing

### Agent Observability

- `AgentRun` / `AgentRunStep` models — Per-turn token counts, status, error, retry_count
- `admin_observability_traces` router — Trace search API
- `live_events` on ChatMessage — Structured activity feed events

### Log Files
- `logs/backend.log` — RotatingFileHandler on `zhanlu` logger
- `logs/frontend.log` — Frontend error log

---

## 21. Testing Strategy

### Backend Tests (`backend/tests/`)
- **1087 test files** — pytest-based
- SQLite in-memory with shared connection for isolation
- `pytest -p no:jaxtyping -p no:cacheprovider` (container)
- No parallel execution (limited RAM)

### Frontend Tests
- **Vitest** for unit tests
- **Playwright** for E2E tests
- `frontend/src/**/*.test.jsx` — Co-located test files

### Key Test Patterns
- `tests/test_kb_reingest.py` — 9 tests with SQLite in-memory
- `frontend/src/api/tokenRefresh.test.js` — Token refresh single-flight
- `frontend/src/pages/Chat.*.test.jsx` — 15+ Chat test files

---

## 22. Key Architectural Decisions & Gotchas

### Design Decisions

1. **@base44/sdk compatibility** — The frontend uses the Base44 SDK for CRUD operations, which means the API must maintain compatibility with its conventions (entity endpoints, auth flow)

2. **Agent loop never routes through AgentRunOrchestrator** — The main chat loop goes directly through `agents.py`; the harness (`AgentRunService`) is only used by SwarmRuntime and Acme delegation

3. **In-process dashboard generation** — `create_fullstack_dashboard` executes in the backend process, not through sandbox. This is a performance optimization but means dashboard generation can't be isolated

4. **Progressive disclosure for skills** — Only skill name + description is injected into system prompts by default. Full skill body loaded on demand to save context tokens

5. **Deterministic-first verification** — Self-eval gate uses deterministic checks (no LLM cost) before optional LLM rubric

6. **Fail-open tool loading** — Dynamic tool selection errors fall back to full tool list, never blocking the agent

7. **Multi-tenant by default** — `org_id` + `app_id` auto-added to every table on startup

### Gotchas

1. **Router ImportError is silent** — If a router module has an import error, FastAPI silently skips it. Always verify with `docker exec zhanlu-backend python -c "from app.routers.<n> import router"`

2. **Bind-mount doesn't reload modules** — Code changes on disk don't affect a running process. Must `docker restart zhanlu-backend zhanlu-sandbox-worker`

3. **`npm run build` is blocked** — Safe-delete shim prevents accidental build. Use `./node_modules/.bin/vite build --emptyOutDir=false`

4. **CJK regex** — Never use `\b` around CJK characters; use lookarounds or wrap ASCII alternatives in `\b`

5. **Feature flags** — All default False. Always `getattr(settings, "FLAG", False)`, never `os.environ.get()`

6. **Agent JSON columns** — Normalize with `coerceStringArray`. Tenant `_apply_tenant` (org_id, app_id); owner `_apply_owner`

7. **Schema pitfall** — `erp_product_sales_details` uses `FNAME` (not `material_name`). Prefer `erp_t_sal_outstock`/`v_sale_order`

8. **Model default** — `deepseek-chat` via `https://api.deepseek.com/v1` (or local Ollama qwen3.5-27b-tools)

9. **Pool size** — SQLAlchemy pool_size=20, max_overflow=40 to handle SSE + polling + background tasks

10. **Poppler not installed** — No `pdftoppm` for PDF thumbnails; PDF preview uses text extraction only

---

## Appendix A: Directory Structure

```
zhanlu_sep_1_n8n/
├── backend/
│   ├── app/
│   │   ├── config.py              # 1500+ line Pydantic Settings
│   │   ├── database.py            # SQLAlchemy engine + Redis
│   │   ├── main.py                # FastAPI app + lifespan
│   │   ├── models/                # 85 SQLAlchemy models
│   │   ├── routers/               # 40 API routers
│   │   ├── services/              # 100+ service modules
│   │   │   ├── synexia/           # FSM cognitive core (25 modules)
│   │   │   ├── compaction/        # 4-layer context management
│   │   │   ├── tool_handlers/     # 100+ tool implementations
│   │   │   ├── rag/               # Knowledge retrieval pipeline
│   │   │   ├── agent_loop/        # Shared loop utilities
│   │   │   ├── forecasting/       # Time-series forecasting (98 .py files)
│   │   │   ├── artifacts/         # Artifact generation (57 .py files)
│   │   │   ├── dashboard_app/     # Live dashboard generation
│   │   │   ├── swarm/             # Multi-agent orchestration
│   │   │   └── ...
│   │   └── utils/
│   ├── skills/                    # 60+ skill directories
│   ├── alembic/                   # 85+ migration files
│   ├── tests/                     # 1087 test files
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.jsx                # Router + providers
│   │   ├── api/                   # API client modules
│   │   ├── components/            # 309 React components
│   │   │   ├── chat/             # 94 files — Chat UI
│   │   │   ├── dashboard/        # 31 files — Dashboard rendering
│   │   │   ├── workflow/         # 11 files — Visual DAG editor
│   │   │   └── ...
│   │   ├── lib/                   # 66 utility modules
│   │   └── pages/                 # 69 page components
│   └── package.json
├── docker-compose.yml             # Full stack definition
├── Makefile                       # Developer commands
├── infra/                         # Postgres/Redis/MinIO configs
└── scripts/                       # 52 Python + 5 shell scripts
```

## Appendix B: Technology Stack Summary

| Layer | Technology |
|-------|-----------|
| **Backend Framework** | FastAPI 0.115.6 + Uvicorn 0.34.0 |
| **ORM** | SQLAlchemy 2.0.36 + Alembic 1.14.0 |
| **Database** | PostgreSQL 16 (primary) / SQLite (dev/test) |
| **Cache / Queue** | Redis 7 (job queue, caching, events) |
| **Object Storage** | MinIO (S3-compatible, artifacts) |
| **LLM** | OpenAI-compatible API (DeepSeek, GPT, Qwen, Kimi, MiniMax) |
| **Embeddings** | sentence-transformers (local, MiniLM) |
| **Vector DB** | ChromaDB (RAG, document indexing) |
| **Frontend Framework** | React 18 + Vite 6 |
| **UI Components** | Radix UI + Tailwind CSS (shadcn/ui pattern) |
| **Charts** | Recharts 2.15 |
| **Workflow Editor** | @xyflow/react 12.11 |
| **Auth** | JWT (PyJWT 2.10) + bcrypt + OTP |
| **Sandbox** | Docker-in-Docker (isolated code execution) |
| **HTTP Client** | httpx 0.28.1 (async LLM calls) |
| **NL2SQL** | sqlglot 25+ (SQL parsing/transformation) |
| **Document Generation** | python-pptx, python-docx, reportlab, openpyxl |
| **Data Science** | pandas, numpy, statsmodels, scikit-learn, xgboost |
| **Monitoring** | Custom tracing (OpenTelemetry-compatible spans) |

---

## Appendix C: Deep-Dive Technical Reference

> **For AI Engineers** — Exact implementation details, code paths, data structures, and algorithms. Not high-level — read the source code with this as your guide.

---

### C.1 Agent Loop — Exact Execution Flow

#### v3 SSE Streaming Loop (`agents.py:_stream_llm_with_tools`)

The primary chat path. Every user message enters `add_message_stream()` (~line 3248 in `agents.py`).

**Pre-loop phase** (before the first LLM call):

```python
# 1. Prepare data source context
data_ctx_extras = prepare_data_source_runtime(conv, db, user_id)
# → Returns {"bound_kb_ids": [...], "schema_descriptions": {...}, "catalog_meta": {...}}

# 2. Build system prompt (5 prompt layers + data context + skill context)
system_prompt = get_system_prompt(agent_name, data_ctx_extras=data_ctx_extras, ...)

# 3. Compute tool_choice for iteration 0
tool_choice = _compute_tool_choice(
    user_content, data_ctx_extras, iteration=0, ...
)
# If data question + bound KBs → tool_choice = {"type": "function", "function": {"name": "ask_data_agent"}}

# 4. Select tools for this turn
tools = get_tools(agent_name, tool_config=conv.tool_config, ...)
# Dynamic loading: core tools + top-K periphery by embedding match

# 5. Build initial messages list
llm_messages = [{"role": "system", "content": system_prompt}] + conversation_history
```

**ReAct loop** (bounded by `MAX_TOOL_ITERATIONS=40`, `TOOL_CALL_HARD_CAP=10`):

```python
for iteration in range(MAX_TOOL_ITERATIONS):
    # ── Call LLM with streaming ──
    async for chunk in stream_chat_completion(
        messages=llm_messages,
        tools=tool_schemas,
        tool_choice=tool_choice,
        model=effective_model,
        stream=True,
    ):
        # Emit SSE events: delta, tool_call, reasoning_content, etc.
        # Buffer: assistant_content (text), raw_tool_calls (structured)
    
    # ── Finish-line forcing ──
    # At T-3 iterations: tool_choice = "none" (model must answer now)
    # At T-1: inject wrap-up nudge message
    
    # ── If no tool_calls → final answer path ──
    if not raw_tool_calls:
        # 1. Hallucination guardrail check
        guardrail = _check_hallucination_guardrail(user_content, data_ctx_extras, ...)
        if guardrail.action == "nudge": continue  # retry with nudge
        
        # 2. PPT turn-guard (if user asked for PPT)
        pptx_guard = pptx_turn_guard(user_content, tool_calls, ...)
        if pptx_guard.action == "nudge": continue
        
        # 3. File deliverable turn-guard (html/docx/xlsx/pdf)
        file_guard = file_turn_guard(user_content, tool_calls, ...)
        if file_guard.action == "nudge": continue
        
        # 4. Self-eval answer verification gate
        gate = _check_answer_verification_gate(user_content, tool_calls, assistant_content, ...)
        if gate.action == "nudge": continue
        
        break  # accept the answer
    
    # ── Parse and execute tool calls ──
    parsed_calls = _parse_tool_calls(raw_tool_calls)
    
    # Dashboard guard interception
    if dashboard_guard_should_block_queries(user_content, tool_calls, iteration):
        # Block execute_query, inject synthetic result forcing create_dashboard
    
    # Data contract confirmation gate
    if contract_confirmation_needed(user_content, tool_calls):
        # Block build tool, inject clarification
    
    # Execute tool batch (parallel when parallel_tool_calls enabled)
    results = await _run_tool_batch(parsed_calls, ...)
    
    # Emit tool_result SSE events
    for result in results:
        yield sse_event("tool_result", {...})
    
    # Append results to messages for next LLM iteration
    llm_messages.append({"role": "assistant", "tool_calls": raw_tool_calls, ...})
    for result in results:
        llm_messages.append({"role": "tool", "tool_call_id": result.call_id, "content": result.content})
```

**Post-loop synthesis**:

```python
# If tool results contain rows (DATA_PRODUCING_TOOLS), synthesize a report
if has_data_rows(tool_results):
    synthesized = await synthesize_report(
        user_content, tool_results, assistant_content, model=effective_model
    )
    # This is a separate LLM call with 120s timeout

# If user asked for file deliverable → finalize_into_artifact
if file_intent:
    artifact = await finalize_into_artifact(synthesized, format=detected_format)
    yield sse_event("artifact", {...})
```

#### Agent Fast Mode (`AGENT_FAST_MODE_ENABLED`)

When enabled (universalized 2026-08-27), applies to ALL models:
- `AGENT_FAST_MAX_TOOL_ITERATIONS` — Lower iteration cap (default 6)
- `AGENT_FAST_GOAL_CONTRACT_ENABLED` — Tighter goal contract
- `AGENT_FAST_VERIFY_NUDGE_MAX` — Fewer verification nudges (default 1)
- `AGENT_FAST_DATA_AGENT_BUDGET_SECONDS` — Shorter data-agent timeout
- `AGENT_FAST_SELF_EVAL_MAX_REPLANS` — 0 (skip re-plan)
- `AGENT_FAST_SELF_EVAL_LLM_GATE_TIMEOUT_S` — 5s (vs 15s default)

Result: 8-15 LLM calls/turn → 3-5, turn time 200s → 42-52s.

---

### C.2 Synexia FSM — State-by-State Implementation

#### State Machine Definition (`synexia/fsm.py`)

```python
class FSMState(str, Enum):
    INIT = "init"
    GOAL = "goal"
    CONTEXT = "context"
    PLAN = "plan"
    GATE = "gate"
    ACT = "act"
    OBSERVE = "observe"
    VERIFY = "verify"
    FINALIZE = "finalize"
    QUALITY_EVAL = "quality_eval"
    DONE = "done"
    FAIL = "fail"
```

#### State Transitions and Logic

**INIT** — Create `Execution` row in DB. Set `current_state="init"`. Fire `on_state_change` callback.

**GOAL** — Parse user message into `TaskSpec`:
- Calls `parse_task_spec(user_message, agent_name, active_skill, conversation_context, db, endpoint)`
- Uses LLM to extract: `task_kind` (data/research/creative/code/general), `entities` (table/column names), `artifact_intents` (file formats), `requires_data` (bool), `skill_override`
- Follow-up reuse: if prior turn has answer datasets and the message is a refinement, inherit prior entities + datasets
- File intent: `_contains_file_intent()` scans for format keywords (pptx/docx/xlsx/pdf/md/html/dashboard)

**CONTEXT** — Assemble `ContextManifest`:
- `assemble_context(db, conversation_id, agent_name, user_message, task_spec, attachments)`
- Components: conversation history (recent messages), agent memory, KB entries, user attachments, system context
- Token budget enforced BEFORE LLM call: `MAX_CONTEXT_TOKENS = 50000`
- Historical file re-injection: `collect_historical_file_urls()` walks past user messages for `/api/uploads/` URLs, bounded by `_HISTORICAL_MAX_USER_MESSAGES=8`, `_HISTORICAL_MAX_URLS=12`, `MAX_HISTORICAL_EXTRACT_CHARS=60000`

**PLAN** — Generate execution DAG:
- `generate_plan(db, execution_id, task_spec, context_manifest, agent_name, failure_context, plan_version, endpoint)`
- LLM proposes steps as JSON; validated for acyclicity
- Plan nodes: `PlanNode(id, execution_id, seq, name, node_type, params, dependencies, status)`
- Node types: `"tool"`, `"skill"`, `"nl2sql"`, `"sandbox"`, `"agent"`, `"synthesize"`, `"data_pipeline"`
- Failure context: when re-planning, receives `[{name, node_type, error}]` dicts for failed nodes
- Plan version: incremented on each re-plan attempt

**GATE** — Policy evaluation on the plan:
- `evaluate_node(plan_node, user_context)` per node
- Policy checks: permission, rate limits, cost estimation, confirmation requirements
- Nodes can be marked `"denied"` (blocked) or `"require_confirm"` (need user approval)

**ACT + OBSERVE** — Execute plan nodes via capability router:
- `execute_plan_nodes(db, execution, plan, user_id, replan_depth, max_replan_depth, data_ctx_extras, on_plan_node, endpoint)`
- Topological sort for execution order
- Per-node dispatch:
  - `"tool"` → `execute_tool(name, args, db, user_id, **ctx)`
  - `"skill"` → `skills_registry.execute(skill_name, args)`
  - `"nl2sql"` → `DataSnapshotService.nl2sql(question, data_source_id)`
  - `"sandbox"` → `SandboxService.submit_job(code, language, timeout)`
  - `"agent"` → `delegate_task(agent_name, question, ...)`
  - `"synthesize"` → `_synth_call_llm(system_prompt, messages, endpoint)`
- Each node: `ObservationRecord(id, execution_id, plan_node_id, success, result_data, artifact_ids, started_at, completed_at)`
- Dynamic ACT re-plan: if a tool/skill node fails, generate a corrective sub-plan and execute it (up to `SYNEXIA_ACT_REPLAN_MAX=2` recursion depth). Data pipeline failures are NEVER re-planned.
- Adaptive planning (`SYNEXIA_ADAPTIVE_PLANNING_ENABLED`): after execution, LLM evaluates whether the plan needs revision based on intermediate results

**VERIFY** — Validate execution outputs:
- Deterministic checks (always on):
  1. `artifact_exists`: every artifact_id in observations resolves to a row in `artifacts` table. **Critical=True**.
  2. `observation_success`: no observation with `success=False` unless originating node was optional. **Critical=True**.
  3. `data_integrity`: each successful observation's `result_data` is non-empty and well-formed. **Critical=False**.
- Optional LLM rubric (`SYNEXIA_VERIFIER_LLM_ENABLED`):
  - 4-check rubric: completeness, quality, source coverage, plausibility
  - Returns `[{"check": str, "ok": bool, "detail": str}]`
  - On any LLM failure/timeout: returns `[]`, deterministic result unchanged
- VERIFY-driven re-plan loop: if a CRITICAL check fails AND failure is attributable to tool/skill nodes, FSM loops back to PLAN with failures as context. Bounded by `SYNEXIA_VERIFY_REPLAN_MAX=1`.
- Contract: VERIFY is non-fatal. `passed=False` lowers confidence score but doesn't block delivery.

**QUALITY_EVAL** — Optional tier-2 quality assessment:
- `SYNEXIA_QUALITY_EVAL_ENABLED` flag
- Combines completeness check + reflexion verdict
- Quality gate for artifact shipping:
  - Interactive chat: threshold 0.4 (`SYNEXIA_QUALITY_GATE_THRESHOLD`)
  - Automation: threshold 0.6 (`SYNEXIA_QUALITY_GATE_THRESHOLD_AUTOMATION`)
  - Below threshold: artifacts held back, user told output was held

**FINALIZE** — Build deliverables:
- Confidence score via `compute_confidence(execution, plan)`:
  - Weighted factors: plan_completion (0.30), observation_success (0.30), artifact_validation (0.15), data_integrity (0.15), policy_compliance (0.10)
- Artifact creation: `Artifact` row (kind=`html_report`) + `ArtifactVersion` + `ArtifactBlob`
- ReportCardPayload: title, KPIs, chart, insights, methodology, key_findings, recommendations, export bar
- Two surfaces: in-chat card (`ReportCard.jsx`) + downloadable artifact
- `user_signal`: `"default"` → card; `"export"` → file download + card

---

### C.3 Tool Execution Pipeline — Complete Call Stack

```
LLM emits tool_calls
    │
    ▼
agents.py: _run_tool_batch()
    ├─ For each tool_call in parallel (if parallel_tool_calls):
    │   │
    │   ▼
    │   execute_tool_with_retry(tool_name, args, db, user_id, **ctx)
    │       │
    │       ├─ Phase 1: Upfront JSON-schema validation
    │       │   validate_tool_args(args, schema) → reject permanent failures immediately
    │       │
    │       ├─ Phase 2: _execute_with_reliability()
    │       │   │
    │       │   ├─ Build call_fn: invoke handler directly (NO inner retry)
    │       │   │   - Async handlers: await handler(args, db, user_id, context=ctx)
    │       │   │   - Sync handlers: asyncio.to_thread(handler, args, db, user_id)
    │       │   │   - If handler returns {"success": False}: raise _ToolResultFailure(result)
    │       │   │
    │       │   ├─ Build llm_repair callback:
    │       │   │   - Skip if _is_arg_fixable_failure() returns False
    │       │   │       (permission denied, blocked by hook, unknown tool = NOT fixable)
    │       │   │   - Skip if TOOL_ARG_REFORMULATION_ENABLED=False
    │       │   │   - Call reformulate_tool_args(tool_name, args, error_str)
    │       │   │     → LLM call: "Given this tool definition and error, suggest corrected args"
    │       │   │   - Return corrected dict if different from original, else None
    │       │   │
    │       │   ├─ Build is_retryable classifier:
    │       │   │   - _ToolResultFailure: classify via _is_retryable(result)
    │       │   │   - Real exceptions: classify via tool_retry.is_retryable()
    │       │   │
    │       │   ├─ LoopState: per-conversation guardrail tracking
    │       │   │
    │       │   └─ run_tool_with_reliability(tool_name, args, call_fn, loop_state, llm_repair, is_retryable)
    │       │       ├─ Retry with exponential backoff (T=0.5s, max=8s)
    │       │       ├─ On exhausted retries: try llm_repair once
    │       │       │   - If reformulation succeeds: retry ONCE with new args
    │       │       │   - If reformulation fails: return last failure
    │       │       └─ Loop guard: ToolLoopGuardController enforces per-tool caps
    │       │
    │       ├─ Phase 3: Unwrap reliability result
    │       │   - Success: return inner result dict
    │       │   - Failure: recover original handler failure dict, mark failure_kind="permanent"
    │       │
    │       └─ Graceful degradation: if reliability layer itself raises,
    │           fall back to ONE direct handler call
    │
    ├─ Tool result persistence (large results → disk)
    │   tool_result_storage.store(call_id, content) → tool_results/{call_id}.json
    │   message content replaced with: "[Result stored: call_id]"
    │
    └─ Tool output truncation
        max_result_size_chars (per-tool or global TOOL_MAX_OUTPUT_CHARS=8000)
        Per-model caps from TOOL_OUTPUT_CAP_BY_MODEL
```

#### Error Classification Heuristics

```python
_PERMANENT_ERROR_MARKERS = (
    "permission denied", "unknown tool", "not found",
    "already exists", "blocked by hook",
)
_TRANSIENT_ERROR_MARKERS = (
    "connect", "timeout", "timed out", "reset",
    "refused", "unavailable", "temporar",
)
# Default: unknown errors are retryable (might be transient)
```

#### Tool Name Alias Resolution

The LLM may hallucinate dotted display names (e.g., `"skills.hub"` instead of `"skills_hub"`). `TOOL_NAME_ALIASES` maps these back:
```python
{"skills.hub": "skills_hub", "skills.sync": "skills_sync", "skills.guard": "skills_guard",
 "skill.provenance": "skill_provenance", "mcp.oauth": "mcp_oauth", ...}
```

---

### C.4 LLM Integration — Exact Request Pipeline

#### `call_llm()` Full Pipeline

```python
async def call_llm(prompt, messages, temperature, response_json_schema, task_type, endpoint):
    # 1. Model routing
    if endpoint: routed_model = endpoint.model_id  # hierarchical LLM pin
    else: routed_model = get_model_for_request(messages, tools_specified, explicit_task)
    
    # 2. Build payload
    payload = build_llm_payload(body, routed_model)
    
    # 3. Sanitize orphan tool_calls (defense-in-depth)
    payload["messages"] = _sanitize_tool_call_pairing(built_messages)
    # Two-pass: strip unmatched assistant.tool_calls + stray tool messages
    
    # 4. Cache check (temperature=0 only)
    cached = get_cached_response(built_messages, model, temperature, schema)
    if cached: return cached
    
    # 5. Per-model tool-output cap (smart_truncate)
    built_messages = smart_truncate(built_messages, model=routed_model)
    
    # 6. Pre-flight context window check
    _est_input_tokens = estimate_messages_tokens(built_messages)
    _tools_tokens = sum(len(json.dumps(t)) // 4 for t in payload.get("tools", []))
    _total_est = _est_input_tokens + _tools_tokens
    if _total_est + LLM_MAX_TOKENS_HARD_CAP > get_context_window(model):
        headroom = max(0, context_window - _total_est)
        if headroom <= 0: raise HTTPException(502, "payload too large")
        payload["max_tokens"] = min(original_max, headroom)
    
    # 7. Parallel tool calls injection
    maybe_enable_parallel_tool_calls(payload, model)
    
    # 8. Provider selection
    if endpoint: providers = [endpoint_provider]
    else:
        providers = get_llm_providers()  # primary + LLM_FALLBACK_PROVIDERS
        if LLM_HEALTH_ROUTING_ENABLED: providers = [select_provider(providers)] + rest
    
    # 9. Execute with failover
    for provider in providers:
        if not is_healthy(provider.name): continue
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
            record_success(provider.name, latency_ms)
            set_cached_response(...)  # temperature=0 only
            return parse_response(resp.json())
        except (HTTPStatusError, RequestError):
            classified = classify_api_error(e)
            record_failure(provider.name, classified.reason)
            if not is_transient(classified): raise HTTPException(...)
            continue  # try next provider
    
    raise HTTPException(502, "all providers exhausted")
```

#### Circuit Breaker (`provider_health.py`)

```python
CIRCUIT_BREAKER_THRESHOLD = 3    # consecutive failures to open
CIRCUIT_BREAKER_COOLDOWN_S = 30.0  # seconds before probe allowed

# State: consecutive_failures, total_successes, total_failures, 
#        total_latency_ms, circuit_open_at, probe_allowed

# is_healthy(name):
#   - failures < threshold → True
#   - cooldown not expired → allow ONE probe, then False
#   - cooldown expired → reset failures, allow probe
```

#### Streaming Response Parsing (`extract_stream_parts`)

```python
def extract_stream_parts(chunk) -> (content_delta, reasoning_delta):
    delta = chunk["choices"][0]["delta"]
    content = delta.get("content", "")
    reasoning = (
        delta.get("reasoning_content")  # DeepSeek-R1
        or delta.get("thinking")         # Claude-style
        or delta.get("reasoning")        # OpenAI o1
        or ""
    )
    return (content, reasoning)
```

#### Token Estimation (`token_estimator.py`)

- **Primary**: tiktoken `cl100k_base` encoder (exact)
- **Fallback**: `len(text) // 4` heuristic (4 chars ≈ 1 token)
- **Padding**: `TOKEN_ESTIMATION_PADDING = 4/3` (multiply estimate by 1.33)
- **Image tokens**: `ZHANLU_IMAGE_TOKEN_ESTIMATE` env var, default 3072

---

### C.5 Data Agent Sub-Loop — Exact Protocol

#### `_ask_data_agent()` in `delegation_tools.py`

```python
async def _ask_data_agent(question, data_source_id, db, user_id, context):
    # 1. Resolve preferred KB
    bound_kb_ids = context.get("bound_kb_ids", [])
    preferred_kb = data_source_id  # if LLM provided it
    
    # If not provided and exactly one KB bound → auto-resolve
    if not preferred_kb and len(bound_kb_ids) == 1:
        preferred_kb = bound_kb_ids[0]
    
    # 2. Build sub-agent system prompt
    sub_prompt = _build_sub_agent_prompt(question, bound_kb_ids, preferred_kb)
    # Contains: available tools list, KB descriptions, schema hints
    
    # 3. Sub-agent tool definitions (4-6 tools)
    tools = get_tool_schemas_for(_DATA_AGENT_TOOLS)
    # list_data_sources, describe_schema, execute_query, 
    # answer_from_database, search_documents, answer_from_documents
    
    # 4. Sub-loop (max 4 iterations, wall-clock budget 60s)
    start = time.monotonic()
    state = {"source_id": preferred_kb, "last_rows": None, "last_sql": None}
    
    for sub_iter in range(4):
        if time.monotonic() - start > DATA_AGENT_BUDGET_SECONDS:
            break  # timeout → return partial results with truncated=True
        
        # Call sub-LLM
        response = await call_llm_with_reliability(
            messages=sub_messages, tools=tools, ...
        )
        
        # If text-only response (no tool_calls) → sub-agent is done
        if not response.tool_calls:
            state["final_text"] = response.content
            break
        
        # Execute sub-tool
        for tc in response.tool_calls:
            result = await execute_sub_tool(tc.name, tc.args, state, db)
            
            if tc.name == "list_data_sources":
                state["source_id"] = result["sources"][0]["id"]  # auto-pick first
            elif tc.name == "describe_schema":
                state["schema"] = result  # store for context
            elif tc.name == "execute_query":
                state["last_rows"] = result["rows"]
                state["last_sql"] = result["sql"]
                state["source_id"] = result.get("source_id", state["source_id"])
            elif tc.name == "answer_from_database":
                state["final_text"] = result["answer"]
                break
    
    # 5. Empty-answer guarantee
    if state.get("last_rows") is not None and not state.get("final_text"):
        # Force synthesis LLM call with rows context
        synthesis = await _call_synthesis_llm(question, state["last_rows"], state["last_sql"])
        state["final_text"] = synthesis
    
    # 6. Return structured result
    return {
        "success": True,
        "answer": state.get("final_text", ""),
        "rows": state.get("last_rows"),
        "sql": state.get("last_sql"),
        "source_id": state["source_id"],
        "source_name": _get_kb_name(db, state["source_id"]),
        "iterations": sub_iter + 1,
        "truncated": time.monotonic() - start > DATA_AGENT_BUDGET_SECONDS,
    }
```

#### Data Agent Tool Schemas

**`list_data_sources`**: No args required. Returns `[{id, name, type, db_type}]` for bound KBs.

**`describe_schema`**: `data_source_id` (required), `table_name` (optional). Returns table/column metadata.

**`execute_query`**: `data_source_id` (required), `sql` (required). Executes SQL against the source DB. Returns `{rows, columns, sql, row_count}`.

**`answer_from_database`**: `data_source_id` (required), `answer` (required), `sql` (optional). Prose answer with optional SQL provenance.

**Garbage data detection**: `_is_trivial_probe(sql)` catches `SELECT * FROM X LIMIT 1/5/10`. `_is_internal_id_column(name)` catches ID columns that should never be chart values.

---

### C.6 Context Compaction — Layer-by-Layer Algorithm

#### Layer 0: Pre-API Prune (`compaction/pre_api_prune.py`)

Deterministic, no LLM call. Runs before every LLM API request.

```python
def smart_truncate(messages, model):
    """Apply per-model tool-output caps BEFORE the API call."""
    cap = get_tool_output_cap(model)  # from TOOL_OUTPUT_CAP_BY_MODEL
    for msg in messages:
        if msg["role"] == "tool" and len(msg["content"]) > cap * 4:  # 4 chars/token
            msg["content"] = msg["content"][:cap * 4] + "\n[...truncated...]"
    return messages

def prune_tool_results_only(messages, current_tokens=None):
    """Drop oldest tool-role messages when context is >75% full."""
    if current_tokens is None:
        current_tokens = estimate_messages_tokens(messages)
    context_window = get_context_window(model)
    if current_tokens < context_window * 0.75:
        return messages, 0
    # Drop oldest tool messages first
    ...
```

#### Layer 1: Microcompact (`compaction/microcompact.py`)

No LLM call. Clears old tool result content for compactable tools.

```python
COMPACTABLE_TOOLS = frozenset({
    "read_file", "write_file", "web_search", "web_extract",
    "execute_code", "list_tools", "list_market_agents", "list_knowledge_bases",
})
DEFAULT_KEEP_RECENT = 5

def microcompact_messages(messages, keep_recent=5):
    """Clear old compactable tool results, keeping the most recent N."""
    all_ids = _collect_compactable_tool_ids(messages)
    if len(all_ids) <= keep_recent:
        return messages, 0
    
    keep_set = set(all_ids[-keep_recent:])
    clear_set = set(all_ids) - keep_set
    
    for msg in messages:
        if msg["role"] == "tool" and msg["tool_call_id"] in clear_set:
            old_len = len(msg["content"])
            msg["content"] = "[Old tool result content cleared]"
            tokens_saved += old_len // 4
    
    return messages, tokens_saved
```

#### Layer 2: Full Compact (`compaction/full_compact.py`)

LLM-based structured summarization. Expensive (one LLM call).

```python
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
COMPACT_TIMEOUT_SECONDS = 120
MAX_COMPACT_RETRIES = 2
MAX_PTL_RETRIES = 3
DEFAULT_PRESERVE_RECENT = 6

async def full_compact(messages, model, preserve_recent=6):
    """Summarize older messages using LLM."""
    # 1. Split: messages_to_compact (old) + messages_to_keep (recent N)
    split_point = len(messages) - preserve_recent
    
    # 2. Build compact prompt with NO_TOOLS_PREAMBLE
    # "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools."
    prompt = get_compact_prompt(custom_instructions)
    
    # 3. Call LLM with retry (PTL = Prompt Too Long)
    for retry in range(MAX_PTL_RETRIES):
        try:
            result = await call_llm(messages=compact_messages + [user_msg], ...)
            break
        except HTTPException as e:
            if "too long" in str(e).lower() or e.status_code == 400:
                # Truncate head and retry
                compact_messages = truncate_head_for_ptl_retry(compact_messages)
            else: raise
    
    # 4. Format and build summary message
    formatted = format_compact_summary(result["response"])
    summary_msg = build_compact_summary_message(formatted, recent_preserved=True)
    # "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted..."
    
    # 5. Return: [summary_msg] + messages_to_keep
    return [summary_msg] + messages_to_keep, True
```

#### Layer 3: Session Memory (`compaction/session_memory.py`)

Persists key facts to `AgentMemory` before compaction drops them.

```python
async def persist_session_memory(conversation_id, agent_name, messages, db, user_id):
    """Extract and persist key facts before compaction drops them."""
    # LLM call: "Extract the key facts, decisions, and data from this conversation"
    facts = await call_llm(messages=extract_prompt + messages, ...)
    
    # Store as AgentMemory rows
    for fact in facts:
        memory = AgentMemory(
            conversation_id=conversation_id,
            agent_name=agent_name,
            content=fact,
            memory_type="session_summary",
        )
        db.add(memory)
```

#### Context Escalation Ladder

When context overflow is imminent, actions applied in order:

```python
CONTEXT_ESCALATION_LADDER = ["compact", "truncate_tool_outputs", "drop_old_tool_messages", "fallback_to_different_model"]

# 0. compact → run auto_compact_if_needed (microcompact → full compact)
# 1. truncate_tool_outputs → apply per-model caps
# 2. drop_old_tool_messages → drop oldest tool-role messages entirely
# 3. fallback_to_different_model → switch to a model with larger context window
```

---

### C.7 SSE Streaming Protocol — Exact Event Sequence

#### Event Types and Payloads

| Event | When Emitted | Payload Shape |
|-------|-------------|---------------|
| `delta` | Per token/chunk from LLM | `{content: str}` |
| `tool_call` | LLM requests tool invocation | `{name: str, args: str, call_id: str}` |
| `tool_result` | Tool execution completed | `{call_id: str, result: dict\|str, success: bool}` |
| `tool_progress` | Heartbeat during long tool execution | `{tool_name: str, elapsed_s: float, message: str}` |
| `activity_step` | Progress indicator (Manus-style) | `{step: int, label: str, status: str, detail: str\|null}` |
| `phase` | FSM state transition | `{headline: str, plan: list\|null}` |
| `fsm_state` | FSM state change | `{state: str, execution_id: str}` |
| `artifact` | Artifact created | `{id: str, type: str, title: str, download_url: str}` |
| `live_event` | Structured activity event | `{type: str, label_key: str, params: dict, ts: float}` |
| `content_preserve` | Before content_replace | `{content: str}` |
| `content_replace` | Full content replacement | `{content: str}` |
| `sources` | Data source citations | `{entries: [{source_id, source_name, rows}]}` |
| `reasoning` | Chain-of-thought tokens | `{content: str}` |
| `done` | Stream complete | `{}` |

#### Heartbeat and Keepalive

```python
_SSE_HEARTBEAT_INTERVAL = 5.0  # seconds (env: ZHANLU_SSE_HEARTBEAT_S)
# Emits SSE comment ": heartbeat\n\n" every 5 seconds
# Prevents proxy idle-kill (Cloudflare, ALB, nginx default 30-60s)

# Long-running tools emit tool_progress every 5s:
_LONG_RUNNING_TOOLS = frozenset({
    "ask_data_agent", "ask_weekly_report", "ask_forecast",
    "web_search", "execute_code", "create_artifact",
    "create_fullstack_dashboard", "run_sandbox_skill", ...
})
```

#### Content Refinement Pattern

When the self-eval gate triggers a refinement:
1. `content_preserve` — snapshot of current content (so frontend can show diff)
2. `content_replace` — full replacement content (refined answer)
3. Frontend: Chat.jsx clears `refining` flag on delta/replace/done

---

### C.8 RAG Pipeline — Exact Retrieval Flow

#### Document Ingestion (`document_ingestion/`)

```
1. File upload → /api/uploads/ → local disk + MinIO
2. Text extraction (by file type):
   - PDF: pypdf.PdfReader → page.extract_text()
   - DOCX: mammoth.convert_to_html() → strip tags
   - HTML: BeautifulSoup.get_text()
   - TXT: raw read
3. Chunking (sentence-boundary split):
   - Split on sentence boundaries (regex: [.!?]+\s)
   - Target chunk size: 500-1000 characters
   - Overlap: 100 characters between chunks
4. Embedding (LOCAL, no cloud):
   - Model: sentence-transformers/MiniLM (cached in /app/data/hf_cache)
   - vector = model.encode(chunk_text)
5. ChromaDB storage:
   - Collection: per-KB or per-session
   - Documents: chunk text
   - Embeddings: MiniLM vectors
   - Metadatas: {source, file_stem, chunk_index, total_chunks}
6. Indexing status: None → "pending" → "indexing" → "ready" | "failed"
```

#### Retrieval (`rag/rag_retriever.py`)

```python
def retrieve_with_context(query, n_results=3, max_chars=1800, collection="industry_reports"):
    """Query ChromaDB and return formatted string for LLM injection."""
    kb = _get_kb()
    results = kb.query(query_texts=[query], n_results=n_results, collection=collection)
    
    # Normalize scores: ChromaDB distance → 0-1 relevance
    # score = exp(-distance)
    results = _normalize_scores(results)
    
    # Build RetrievedChunk objects
    chunks = [
        RetrievedChunk(
            text=r["text"],
            source=r["metadata"]["source"],
            file_stem=r["metadata"]["file_stem"],
            chunk_index=r["metadata"]["chunk_index"],
            total_chunks=r["metadata"]["total_chunks"],
            score=r["score"],
        )
        for r in results
    ]
    
    # Format for prompt injection
    return "\n".join(chunk.formatted(max_chars=600) for chunk in chunks)
    # → "[Source: Acme_SPEC.md](3/8) excerpt text..."
```

#### Chat Upload RAG (Large Files)

When `RAG_UPLOADS_ENABLED=True`:
- Files < `RAG_UPLOADS_INLINE_MAX_CHARS=6000`: full text injected inline
- Files > 6000 chars: indexed into ChromaDB, top-K chunks retrieved per turn
- `RAG_UPLOADS_TOP_K=8` chunks per turn
- `RAG_UPLOADS_MAX_FILES_INLINE=3`: more than 3 files → switch to retrieval
- Collection: `chat_uploads`
- Idempotent: same session + file → skip re-indexing

---

### C.9 Confidence Scoring — Exact Formula

```python
def compute_confidence(execution, plan) -> (score, factors):
    weights = {
        "plan_completion": 0.30,     # completed_nodes / total_nodes
        "observation_success": 0.30, # successful_obs / total_obs
        "artifact_validation": 0.15, # real: artifact has non-empty blob?
        "data_integrity": 0.15,      # from VERIFY data_integrity check
        "policy_compliance": 0.10,   # allow=1.0, require_confirm=0.7, denied=0.0
    }
    
    score = sum(factor_score * weight for each factor)
    # Range: 0.0 - 1.0
    
    # Quality gate: if score < threshold → hold artifacts
    threshold = 0.4  # interactive chat
    threshold = 0.6  # automation runs (stricter)
```

---

### C.10 Tool Registry — Registration and Dispatch

#### `ToolEntry` Data Structure

```python
@dataclass
class ToolEntry:
    name: str                               # Canonical name (underscore form)
    schema: dict                            # OpenAI function-calling format
    handler: Callable                       # (args, db, user_id, **ctx) -> dict
    category: str = "general"               # Grouping tag
    toolset: str = "general"                # Hermes-style toolset grouping
    enabled_by_default: bool = True         # Visible without explicit opt-in
    requires_config: list[str] = []         # Legacy config keys
    requires_env: list[str] = []            # Phase 1: env var names needed
    is_async: bool = False                  # Auto-detected if not set
    check_fn: Optional[Callable] = None     # Runtime availability check
    max_result_size_chars: Optional[int]    # Per-tool output cap
    dynamic_schema_overrides: Optional[Callable] = None  # Runtime schema changes
    description: str = ""
    emoji: str = ""
```

#### Dynamic Tool Selection

```python
TOOL_LOADING_MODE = "dynamic"  # or "all" (legacy)
TOOL_LOADING_CORE = [
    "memory", "project_memory", "session_search", "clarify",
    "read_file", "write_file", "execute_code", "sandbox_code",
    "web_search", "web_extract", "search_skills", "list_skills",
    "list_knowledge_bases", "delegate_task", "send_message", "interrupt",
    "sql_query", "ask_data_agent", "comprehensive_data",
    "create_dashboard", "create_fullstack_dashboard", "edit_dashboard",
    "create_artifact", "edit_artifact", "load_skill_body", "skill_manager",
    "list_fullstack_dashboards", "approval",
]
TOOL_LOADING_PERIPHERY_TOP_K = 12

# Per turn:
# 1. Always include core tools
# 2. Embed user message → cosine similarity against tool descriptions
# 3. Add top-K most relevant periphery tools
# 4. Fail-open: any error → return full tool list
```

#### Per-Tool Call Caps (Loop Guard)

```python
TOOL_CALL_CAPS = {
    "memory": 1,          # idempotent-write — one call is enough
    "ask_data_agent": 6,  # serial data queries; one comprehensive query should suffice
    "fetch_data_batch": 3, # direct SQL fast path; up to 8 queries per call
    "interrupt": 2,       # UX handoff; repeated = stuck agent
    "clarify": 3,         # repeated = stuck agent
}
```

---

### C.11 File Intent Detection — Exact Algorithm

```python
def detect_file_intent(message: str) -> Optional[FileFormat]:
    """Detect whether user explicitly asked for a file deliverable."""
    # 1. READ guard: if message is a read request, return None
    if is_file_read_request(message):
        return None  # "read this docx" → NOT a create request
    
    # 2. CONVERSION target: "convert this docx to pdf" → return "pdf"
    convert_target = detect_convert_target(message)
    if convert_target:
        return convert_target
    
    # 3. Format pattern matching
    for fmt, pattern in _COMPILED.items():
        if pattern.search(message):
            return fmt
    
    return None

# READ verb detection:
_READ_VERB_RE = re.compile(
    r"\b(?:read|summarize|analy[sz]e|explain|extract|translate|"
    r"review|parse|interpret|describe|outline|recap|digest|preview|inspect|"
    r"open|highlight|break\s+down|go\s+through|look\s+at|look\s+over|"
    r"walk\s+me\s+through|tell\s+me\s+about|tell\s+me\s+what'?s\s+in|"
    r"what'?s\s+in|contents?\s+of|details?\s+of)\b"
)
```

---

### C.12 Embedding System — Exact Pipeline

```python
def get_embedding(text, model=None) -> list[float] | None:
    """Return embedding vector, or None on any failure (fail-open)."""
    if not text or not OPENAI_API_KEY or not MEMORY_EMBEDDINGS_ENABLED:
        return None
    
    payload = {"model": model or EMBEDDING_MODEL, "input": text[:8000]}
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{OPENAI_BASE_URL}/embeddings", ...)
            data = resp.json()
        return data["data"][0]["embedding"]
    except Exception:
        return None  # fail-open: callers MUST handle None

# Memory search fallback when embedding unavailable:
# → lexical token-overlap scoring (no semantic, but works offline)
```

---

### C.13 Message Sanitization — Defense-in-Depth

```python
def _sanitize_tool_call_pairing(messages) -> list[dict]:
    """Drop orphan assistant.tool_calls and stray tool messages.
    
    Why: OpenAI/DeepSeek returns HTTP 400 for orphan tool_calls.
    This causes: record_failure → circuit-breaker opens → 
    all providers exhausted → 502 → "agent not responding"
    
    Two-pass algorithm:
    
    Pass 1 (forward): for each assistant msg with tool_calls,
    walk forward through contiguous tool messages. Any unmatched
    tool_call_ids are stripped from assistant.tool_calls.
    If entire list stripped, remove tool_calls key but keep text.
    
    Pass 2 (backward): any tool message whose tool_call_id
    does not appear in ANY preceding assistant.tool_calls
    is removed entirely.
    
    Returns new list (never mutates input).
    """
```

---

### C.14 LLM Placeholder Detection

```python
def _looks_like_llm_placeholder(value) -> bool:
    """Detect LLM-emitted placeholder values that bypass naive fallbacks.
    
    Caught examples: "TOOL_CONTEXT.project_id", "$org_id", "<id>",
    "None", "undefined"
    
    Used to override LLM's arg with runtime-injected TOOL_CONTEXT value
    before the row hits a FK constraint.
    """
    if lower in {"null", "none", "undefined", "n/a", "na", "nan"}: return True
    if "." in s: return True       # TOOL_CONTEXT.x
    if s.startswith("$") or s.endswith("$"): return True
    if "{{" in s or "}}" in s or "%" in s: return True
    if s.startswith("<") or s.endswith(">"): return True
    return False
```

---

### C.15 Answer Verification Gate — Exact Checks

```python
async def _check_answer_verification_gate(user_content, tool_calls, assistant_content, attempts, budget_remaining, catalog_meta, agent_name):
    """Universal self-eval gate: deterministic + optional LLM rubric."""
    
    # Skip conditions:
    if attempts >= VERIFY_NUDGE_MAX: return DisclosureResult(...)
    if budget_remaining <= 2: return PassResult()  # too few iterations left
    
    # Deterministic checks:
    # 1. Is the answer metadata-only? (just schema/column names, no real data)
    if is_metadata_only_rows(tool_results): return NudgeResult("No real data in answer")
    
    # 2. Is the answer empty? (no content after all tool calls)
    if not assistant_content or len(assistant_content.strip()) < 20:
        return NudgeResult("Answer is empty")
    
    # 3. Degenerate values? (all zeros, same value repeated)
    if has_degenerate_values(tool_results): return NudgeResult("Values look degenerate")
    
    # 4. Lexical dimension coverage? (user asked for N dimensions, answer covers M < N)
    requested_metrics = extract_requested_metrics(user_content)
    covered_metrics = extract_covered_metrics(assistant_content)
    if len(covered_metrics) < len(requested_metrics):
        return NudgeResult(f"Missing dimensions: {set(requested_metrics) - set(covered_metrics)}")
    
    # 5. Placeholder non-answer? ("I cannot", "As an AI", etc.)
    if is_placeholder_non_answer(assistant_content): return NudgeResult(...)
    
    # Optional LLM rubric (4-check):
    if SELF_EVAL_LLM_GATE_ENABLED:
        result = await evaluate_answer(user_content, tool_results, assistant_content)
        # Returns: {"status": "COMPLETE"|"INCOMPLETE"|"IMPOSSIBLE", "gaps": [...], "suggested_fix": "..."}
        if result["status"] == "INCOMPLETE":
            return NudgeResult(result["suggested_fix"])
        if result["status"] == "IMPOSSIBLE":
            return DisclosureResult(build_gap_disclosure(result["gaps"]))
    
    # Data-sufficient fast-path: skip gate after 1 nudge if agent has data + >=200 chars
    if has_data and len(assistant_content) >= SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE:
        return PassResult()
    
    return PassResult()
```

---

## 23. Prompt Engineering Patterns

The system prompt is assembled in **two phases**: base prompt selection and dynamic layer injection. This is the most critical surface for an AI engineer to understand — every agent behavior can be steered by modifying these layers.

### 23.1 Five-Layer Constitutional Prompt (User-Created Agents)

User-created agents (via Agent Builder) use a **constitutional prompt** assembled from 5 ordered fields on the `AgentApp` model:

| Layer | Field | Purpose | Example |
|-------|-------|---------|---------|
| 1 — Identity | `prompt_identity` | Who the agent is, personality, domain | "You are a C5/C9 industrial market analyst..." |
| 2 — Boundary | `prompt_boundary` | What the agent will NOT do | "Never make up data. Do not answer questions about HR." |
| 3 — Reasoning | `prompt_reasoning` | How the agent should think | "Always verify SQL results against the original question..." |
| 4 — Tools | `prompt_tools` | How the agent should use tools | "Prefer execute_query over web_search for data questions..." |
| 5 — Output | `prompt_output` | Formatting requirements | "Respond in markdown with tables. Always cite data sources." |

Assembly function: `assemble_user_agent_prompt(agent_app)` in `backend/app/services/agent_prompts.py` (line ~2350). Layers are concatenated with `\n\n` separators; empty/whitespace-only layers are skipped.

### 23.2 System-Agent Hardcoded Prompts

Built-in agents (agent_builder, skill_agent, research_analyst, etc.) use hardcoded prompts defined in `backend/app/services/agent_prompts.py`:

```python
# Priority order:
# 1. AgentDefinitions system (.md files or builtin registry)
# 2. Hardcoded prompts (legacy fallback for system agents)
# 3. 5-layer assembly from AgentApp record (user-created)
# 4. Generic fallback
```

Key system prompts:
- `AGENT_BUILDER_SYSTEM_PROMPT` — instructs the LLM how to create/edit other agents
- `SKILL_AGENT_SYSTEM_PROMPT` — skill execution with methodology injection
- Research analyst prompt — includes C5/C9 domain knowledge block

### 23.3 Dynamic Prompt Builder — 8 Injection Layers

After the base prompt is selected, `build_system_prompt()` from `backend/app/services/dynamic_prompt_builder.py` injects 8 runtime layers:

```python
def build_system_prompt(
    *,  # keyword-only
    base_prompt: str,
    db: Session,
    agent_app_id: str,
    conversation_id: str,
    user_id: str | None = None,
    agent_app: Any = None,
    workspace_path: str | None = None,
    project_id: str | None = None,
    inject_memory: bool = True,        # 1
    inject_todos: bool = True,          # 2
    inject_coding_context: bool = True, # 3
    inject_learning_graph: bool = True, # 4
    inject_recipes: bool = True,        # 5
    inject_user_profile: bool = True,   # 6
    inject_role_context: bool = True,   # 7
    inject_entity_masters: bool = True, # 8
) -> str:
    prompt = base_prompt
    if inject_memory:       prompt = _inject_memory(prompt, db, agent_app_id, user_id, project_id=project_id)
    if inject_todos:        prompt = _inject_todos(prompt, db, conversation_id)
    if inject_coding_context:  prompt = _inject_coding_context(prompt, agent_app, workspace_path)
    if inject_learning_graph:  prompt = _inject_learning_graph(prompt, agent_app_id)
    if inject_recipes:      prompt = _inject_recipes(prompt, agent_app_id)
    if inject_user_profile: prompt = _inject_user_profile(prompt, agent_app_id, user_id)
    if inject_role_context: prompt = _inject_role_context(prompt, db, user_id)
    if inject_entity_masters: prompt = _inject_entity_masters(prompt, db, agent_app, project_id=project_id)
    return prompt
```

**After** `build_system_prompt()`, the router in `agents.py` (lines 6780–6880) appends further blocks:
- **Follow-up context** — `format_followup_context_block()` injects previous conversation context
- **Skill methodology** — Skill-specific prompt appended when a skill is active
- **Schema-aware protocol** — Data source schema hints for NL2SQL
- **Research analyst directive** — Domain-specific analytical instructions
- **Conversation tone block** — Universal behavioral constraints
- **Initiative block** — Proactive behavior guidelines
- **Runtime context block** — Current time, user timezone, active project

### 23.4 Tool Description Format

Tools are described to the LLM via OpenAI-compatible function schemas. Each tool has:

```json
{
  "type": "function",
  "function": {
    "name": "execute_query",
    "description": "Execute a SQL query against a connected data source...",
    "parameters": {
      "type": "object",
      "properties": {
        "query": { "type": "string", "description": "SQL query to execute" },
        "data_source_id": { "type": "string", "description": "UUID of the data source" }
      },
      "required": ["query", "data_source_id"]
    }
  }
}
```

Tool descriptions are the **primary control surface** for steering agent behavior. Key patterns:
- **Mandatory parameters** force the LLM to gather info before calling (e.g., `data_source_id` requires listing sources first)
- **Description-level guidance** embedded in parameter descriptions ("Use ISO 8601 dates")
- **Per-agent tool subsets** — only tools in `allowed_tools` are included in the schema

### 23.5 Few-Shot Example Injection

Few-shot examples are injected at multiple points:

1. **OntologyFeedback** (`backend/app/services/ontology_feedback.py`) — User corrections to concept mappings are stored and injected as few-shot examples in the NL2SQL prompt: `"Previous successful mappings: user_input='销量' → column='FNAME'"`
2. **Skill methodology** — Each skill includes worked examples in its methodology prompt
3. **Reformulation retry** (`backend/app/services/tool_retry.py:153-241`) — After a tool call fails, the LLM is shown the failed call + error and asked to reformulate arguments

### 23.6 Prompt Versioning & A/B Testing

- Schema snapshots stored in `SchemaSnapshot` model allow prompt regression detection
- `OntologyCorrection` model tracks user feedback that modifies prompt behavior
- Agent prompts are versioned via `AgentApp.updated_at` — prompt changes are reflected in next conversation turn

---

## 24. Agent Harness — Orchestrator & Run Service

The **Agent Harness** is the structured execution engine that replaces the raw conversational loop for programmatic agent runs. This is the most architecturally significant subsystem for production AI engineering — it provides checkpoint/resume, budget enforcement, and structured output.

### 24.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Harness Architecture                │
│                                                             │
│  ┌───────────────┐    ┌────────────────────┐               │
│  │ AgentRunService│───▶│AgentRunOrchestrator│               │
│  │ (persistence,  │    │ (execution engine) │               │
│  │  queue, resume)│    │                    │               │
│  └───────┬───────┘    └────────┬───────────┘               │
│          │                     │                             │
│          ▼                     ▼                             │
│  ┌───────────────┐    ┌────────────────────┐               │
│  │ AgentRun DB   │    │ ToolLoopGuardCtrl  │               │
│  │ AgentRunStep  │    │ (tool_loop_        │               │
│  │ (checkpoint)  │    │  guardrails.py)    │               │
│  └───────────────┘    │ IterationBudget    │               │
│                       │ (iteration_        │               │
│                       │  budget.py)        │               │
│                       └────────────────────┘               │
│                                                             │
│  Event Pipeline:                                            │
│  run_start → [llm_call → tool_call → tool_result]*         │
│            → forced_synthesis → run_finish                  │
└─────────────────────────────────────────────────────────────┘
```

### 24.2 AgentRunOrchestrator — Core Loop

**File**: `backend/app/services/harness/orchestrator.py`

```python
class AgentRunOrchestrator:
    """Structured agent execution with guard controllers and budget enforcement."""
    
    def __init__(self, agent_name, task, system_prompt, tool_schemas,
                 allowed_tools, llm_fn, tool_dispatcher, db, user_id, context,
                 denied_tools=None, max_iterations=8, temperature=0.3,
                 event_sink=None, run_store=None, pre_call_prep=None,
                 persist_result_str=False, apply_turn_budget=None, run_id=None):
        ...
    
    async def run(self) -> RunResult:
        # 1. Emit run_start
        # 2. Create ToolLoopGuardController + IterationBudget
        # 3. Build initial messages [system, user]
        # 4. Iterate up to max_iterations:
        #    a. Optional pre_call_prep (sanitize/prune messages)
        #    b. Call llm_fn — on failure, return error result immediately
        #    c. If no tool calls → final_text = content, break
        #    d. Append assistant message with tool calls
        #    e. For each tool call: guard → denied → allowed → dispatch → result
        #    f. apply_turn_budget enforcement
        #    g. Guardrail halt check (looping tool detection)
        # 5. Forced synthesis turn if no final_text (tools=None + "stop" instruction)
        # 6. Return RunResult via _finish()
```

Key design decisions:
- **`llm_fn` is injected** — allows testing with mock LLM, or routing to different providers
- **`tool_dispatcher` is injected** — decouples from the main router's execute_tool
- **`pre_call_prep` hook** — called before each LLM call for message sanitization (e.g., remove stale tool results)
- **Forced synthesis** — if the loop exhausts iterations without a final answer, one more LLM call with `tools=None` forces a text-only synthesis

### 24.3 AgentRunService — Persistence & Lifecycle

**File**: `backend/app/services/harness/run_service.py`

```python
class AgentRunService:
    async def start_run(self, agent_name, task, context, ...) -> str:
        """Create run record, then execute inline or enqueue."""
        record = await self.create_run_record(...)  # status="queued"
        if inline:
            return await self._execute_inline(record, ...)
        else:
            await enqueue_task(record.run_id)
            return record.run_id
    
    async def collect_run(self, run_id, timeout=300) -> AgentRun:
        """Poll until terminal status (completed/failed)."""
    
    async def resume_run(self, run_id) -> RunResult:
        """Resume crashed run from last AgentRunStep.messages_snapshot."""
```

State machine:
```
queued → running → completed
                  → failed
                  → crashed (resume eligible)
```

### 24.4 Checkpoint & Resume

Each iteration writes an `AgentRunStep` row with:
- `messages_snapshot` — full conversation state at that point
- `tool_calls_log` — JSON array of tool calls made
- `iteration` — which iteration this represents

On crash recovery, `resume_run()`:
1. Finds the latest `AgentRunStep` with a `messages_snapshot`
2. Reconstructs the orchestrator with the snapshot as initial messages
3. Continues execution from that point

### 24.5 Guard Controllers

**ToolLoopGuardController** (`backend/app/services/tool_loop_guardrails.py`):
- Detects **looping tool calls** — same tool called N times with similar args
- Enforces **per-tool caps** — e.g., `memory:1, ask_data_agent:2, interrupt:2, clarify:3`
- `after_call(tool_name)` increments counter, checks halt condition
- Returns `should_halt=True` when budget exceeded → orchestrator breaks loop

**IterationBudget** (`backend/app/services/iteration_budget.py`):
- Global iteration counter with `max_iterations` cap
- Remaining budget passed to `_check_answer_verification_gate` for adaptive evaluation

### 24.6 How the Harness Differs from the Main Chat Loop

| Feature | Main Chat Loop (agents.py) | Agent Harness |
|---------|---------------------------|---------------|
| Execution | Raw LLM loop with inline tool dispatch | Orchestrated with guard controllers |
| Persistence | Message rows only | AgentRun + AgentRunStep rows |
| Resume | No (conversation continues) | Yes (from messages_snapshot) |
| Budget | MAX_TOOL_ITERATIONS=40, TOOL_CALL_HARD_CAP=10 | max_iterations=8 (configurable) |
| Forced synthesis | Not guaranteed | Always (tools=None fallback) |
| Streaming | SSE to frontend | EventSink abstraction |
| Use cases | User chat, research loop | Acme ask_*, programmatic runs |

### 24.7 Current Gaps & Roadmap

| Gap | Status | Impact |
|-----|--------|--------|
| VERIFY state is a no-op logger | Known | No replan/reflection loop |
| Plan nodes execute serially | Known | Topological sort exists but not used for parallelism |
| `capability_router` "agent" node type | Unimplemented | Falls through to tool dispatch |
| No sub-agent fork/join | Known | Cannot parallelize sub-tasks |
| SwarmRuntime is stub | Empty directory | No multi-agent orchestration |

---

## 25. LLM Cost Model & Token Budget

### 25.1 Model Selection & Task Routing

**File**: `backend/app/services/model_router.py`

The platform uses **heuristic task classification** to route requests to different models:

```python
def classify_task(messages, tools_specified) -> str:
    """Classify into: simple_chat | reasoning | tool_use | code_gen | document_gen"""
    # Keyword matching (including CJK keywords)
    # Returns task type string
    
def route_model(task_type) -> str:
    """Look up task type in routing table → model name."""
    # Falls back to settings.LLM_MODEL if no mapping
```

Configuration: `settings.MODEL_TASK_ROUTING` — JSON string mapping task types to model names:
```json
{
  "simple_chat": "deepseek-chat",
  "reasoning": "deepseek-reasoner",
  "tool_use": "deepseek-chat",
  "code_gen": "deepseek-chat",
  "document_gen": "deepseek-chat"
}
```

### 25.2 Provider Fallback & Health Routing

**File**: `backend/app/services/provider_health.py`

Circuit-breaker pattern for multi-provider resilience:

| Parameter | Value | Description |
|-----------|-------|-------------|
| `CIRCUIT_BREAKER_THRESHOLD` | 3 | Consecutive failures before circuit opens |
| `CIRCUIT_BREAKER_COOLDOWN_S` | 30.0 | Seconds before retry after circuit opens |

```python
def select_provider(providers) -> ProviderInfo:
    """Pick healthiest provider by average latency among healthy ones."""
    # Providers with open circuit breakers are excluded
```

Configuration: `settings.LLM_FALLBACK_PROVIDERS` — JSON array:
```json
[
  {"name": "deepseek-primary", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-...", "model": "deepseek-chat"},
  {"name": "openai-fallback", "base_url": "https://api.openai.com/v1", "api_key": "sk-...", "model": "gpt-4o"}
]
```

When `LLM_HEALTH_ROUTING_ENABLED=True`, providers are reordered by health score before each call.

### 25.3 Token Budget Architecture

**Three-layer clamping** (`agents.py` line 9863–9929):

```
Request max_tokens
    │
    ▼
┌─────────────────────────────┐
│ Layer 1: Per-model ceiling   │  LlmModel.max_output_tokens
│ (hard cap from DB)           │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Layer 2: Dynamic headroom    │  context_window - estimated_input_tokens - safety_margin
│ (prevents context overflow)  │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Layer 3: Global hard cap     │  settings.LLM_MAX_TOKENS_HARD_CAP = 4096
│ (safety net)                 │
└─────────────────────────────┘
```

Key constants:

| Setting | Default | Purpose |
|---------|---------|---------|
| `LLM_MAX_TOKENS_HARD_CAP` | 4096 | Global max_tokens ceiling |
| `LLM_SYNTH_MAX_TOKENS` | 6144 | Synthesis/report output budget |
| `MODEL_CONTEXT_WINDOWS` | `""` | JSON per-model context window sizes |
| `TOKEN_ESTIMATION_PADDING` | 4/3 | Conservative token count multiplier |

### 25.4 Token Estimation

**File**: `backend/app/services/compaction/token_estimator.py`

```python
def estimate_text_tokens(text) -> int:
    """Uses tiktoken (cl100k_base) when available, falls back to len(text)//4."""
    
def estimate_message_tokens(message) -> int:
    """Handles all Zhanlu formats: text, multimodal, tool_calls, images."""
    # Vision images: _DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE = 3072
    
def estimate_messages_tokens(messages) -> int:
    """Sum per-message estimates with TOKEN_ESTIMATION_PADDING (×4/3)."""
```

### 25.5 Cost Optimization Patterns

1. **Compaction engine** — 3-layer context compression reduces token usage by 40-60% on long conversations
2. **Qwen fast-path** — `BI_KG_QWEN_FAST_PATH=True` augments system prompt with cached knowledge, reducing RAG tool calls
3. **Per-tool caps** — `memory:1, ask_data_agent:2` prevent expensive tool loops
4. **Data-sufficient fast-path** — skips self-eval gate after 1 nudge when answer has data + ≥200 chars
5. **Tier routing** — simple chat uses cheap models, reasoning uses expensive ones
6. **Fixed-temperature model detection** — `LLM_FIXED_TEMPERATURE_MODELS` avoids sending invalid `temperature` param to models like o1/o3/kimi-k2

### 25.6 Estimated Per-Turn Cost (DeepSeek Pricing)

| Task Type | Avg Input Tokens | Avg Output Tokens | Cost (DeepSeek) |
|-----------|-----------------|------------------|-----------------|
| Simple chat | 2,000 | 500 | ~$0.001 |
| Tool use (1 tool) | 5,000 | 1,000 | ~$0.003 |
| Data agent (4 sub-iterations) | 20,000 | 3,000 | ~$0.012 |
| Research loop (3 iterations) | 30,000 | 5,000 | ~$0.020 |
| Document gen (PPT/DOCX) | 15,000 | 4,000 | ~$0.010 |
| Synthesis report | 10,000 | 2,000 | ~$0.006 |

*Note: Costs scale linearly with model price. GPT-4o is ~15× more expensive per token than DeepSeek.*

---

## 26. Sandbox Isolation Model

### 26.1 Docker-Out-of-Docker (DooD) Architecture

The sandbox uses Docker-outside-of-Docker, not Docker-in-Docker. The sandbox worker runs as a sibling container with Docker socket access, spawning isolated containers for each task.

```
┌─────────────────────────────────────────────────────────┐
│  Docker Compose Host                                    │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Backend  │  │ Frontend │  │ Sandbox Worker        │  │
│  │ :5002    │  │ :8088    │  │ Docker CLI access     │  │
│  └────┬─────┘  └──────────┘  │ (via /var/run/docker  │  │
│       │                      │  .sock mount)          │  │
│       │ enqueue              └──────────┬─────────────┘  │
│       ▼                                 │ docker run     │
│  ┌──────────┐                          ▼                 │
│  │ Redis    │  ◀──── BRPOP ────┌──────────────────┐     │
│  │ :6300    │                  │ Sandbox Container │     │
│  └──────────┘                  │ --network none    │     │
│                                │ --read-only       │     │
│                                │ --memory 1g       │     │
│                                │ --cpus 1          │     │
│                                │ --cap-drop ALL    │     │
│                                └──────────────────┘     │
└─────────────────────────────────────────────────────────┘
```

### 26.2 Container Security Flags

**File**: `backend/app/services/sandbox/container_manager.py` (lines 131-144)

```python
docker_cmd = [
    _DOCKER_PATH, "run",
    "--name", container_name,
    "--rm",                     # Auto-remove after exit
    "--network", "none",        # NO network access
    "--read-only",              # Read-only root filesystem
    "--memory", memory,         # Memory limit (default "1g")
    "--cpus", cpus,             # CPU limit (default "1")
    "--cap-drop", "ALL",        # Drop ALL Linux capabilities
    "--tmpfs", "/tmp:rw,size=256m",   # Writable /tmp (256MB)
    "-v", f"{mount_input}:/input:ro",   # Input: read-only
    "-v", f"{mount_output}:/output:rw", # Output: read-write
    "-w", "/output",
]
```

**Security guarantees**:
- **No network** — container cannot phone home, exfiltrate data, or call external APIs
- **Read-only filesystem** — cannot modify system binaries or install packages
- **No capabilities** — cannot mount filesystems, change hostname, load kernel modules
- **Memory/CPU limits** — prevents resource exhaustion attacks
- **Auto-remove** — no leftover containers after execution

### 26.3 LLM Proxy for Sandboxed Skills

Since sandboxed containers run `--network none`, they cannot call the LLM API directly. The sandbox worker provides a **Unix domain socket proxy**:

**File**: `backend/sandbox_worker/main.py` (lines 424-482)

```
Sandbox Container ─── Unix Socket ──▶ LLM Proxy Thread ─── HTTP ──▶ LLM API
         (no network)              (in worker process)        (has network)
```

The proxy:
1. Listens on `SANDBOX_LLM_PROXY_SOCKET` (Unix domain socket)
2. Accepts HTTP requests from sandbox containers (mounted as a volume)
3. Forwards to the configured LLM API endpoint
4. Returns responses back through the socket

This allows skill-driven containers to use LLM calls for dynamic content generation while maintaining network isolation.

### 26.4 Timeout & Output Caps

| Parameter | Value | Location |
|-----------|-------|----------|
| `timeout_seconds` | 120 (default) | `SandboxJob` model, `SandboxService.create_job` |
| `MAX_OUTPUT_FILE_SIZE` | 50 MB | `container_manager.py:24` |
| `MAX_TOTAL_OUTPUT_SIZE` | 200 MB | `container_manager.py:25` |
| `/tmp` tmpfs size | 256 MB | Docker run flag |

On timeout, the worker force-removes the container:
```python
except subprocess.TimeoutExpired:
    subprocess.run([_DOCKER_PATH, "rm", "-f", container_name], ...)
```

### 26.5 Image Map (Task → Sandbox Image)

| Task Type | Docker Image | Purpose |
|-----------|-------------|---------|
| `pptx` | `zhanlu-sandbox-pptx:latest` | PowerPoint generation |
| `docx` | `zhanlu-sandbox-office:latest` | Word/Excel generation |
| `xlsx` | `zhanlu-sandbox-office:latest` | Excel generation |
| `skill_runner` | `zhanlu-sandbox-skill:latest` | Skill execution (most capable) |
| `default` | `zhanlu-sandbox-python:latest` | General Python execution |

Base image (`Dockerfile.sandbox`): Python 3.12-slim, non-root `sandboxuser`, pandas/numpy/openpyxl/python-pptx/python-docx/reportlab/matplotlib.

---

## 27. Adding a New Tool / Skill — Step-by-Step

### 27.1 Adding a New Tool

**Step 1: Create the tool handler**

Create a new file in `backend/app/services/tool_handlers/` (e.g., `my_tool.py`):

```python
"""Handler for the my_tool tool."""

async def handle_my_tool(
    query: str,
    data_source_id: str | None = None,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Execute my_tool.
    
    Args:
        query: The user's query string
        data_source_id: Optional data source UUID
        
    Returns:
        {"success": True, "result": "..."} or {"success": False, "error": "..."}
    """
    # Implementation here
    return {"success": True, "result": "Tool output"}
```

**Step 2: Define the tool schema**

In `backend/app/services/agent_tools.py`, add the function definition to the tool list:

```python
MY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "Description that helps the LLM decide when to use this tool. Be specific about when and how.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The query to process. Use specific field names when known."
                },
                "data_source_id": {
                    "type": "string",
                    "description": "UUID of the data source. Use list_data_sources first if unknown."
                }
            },
            "required": ["query"]
        }
    }
}
```

**Step 3: Register the handler**

Tools are registered via the `ToolRegistry` (`backend/app/services/tool_registry.py`). Each tool handler module calls `register()` at import time:

```python
# In backend/app/services/tool_handlers/my_tool.py
from app.services.tool_registry import registry

async def handle_my_tool(query: str, **kwargs) -> dict:
    ...

# Register at module level
registry.register("my_tool", handle_my_tool, schema=MY_TOOL_SCHEMA)
```

The `execute_tool()` function in `agent_tools.py` checks the registry first, then falls back to a local `_CRUD_DISPATCH` dict for legacy CRUD handlers. Sync handlers are automatically wrapped with `asyncio.to_thread()`.

**Step 4: Add to allowed tools**

In `backend/app/config.py`, add to `DEFAULT_TOOLS_BY_AGENT` if the tool should be available by default:

```python
DEFAULT_TOOLS_BY_AGENT = {
    "default": ["my_tool", ...],
    ...
}
```

**Step 5: Add per-tool caps** (optional but recommended)

In `agents.py`, add to the per-tool cap dictionary:

```python
PER_TOOL_CAPS = {
    "my_tool": 2,  # Max 2 calls per conversation turn
    ...
}
```

**Step 6: Test**

```bash
# Verify import
docker exec zhanlu-backend python -c "from app.services.tool_handlers.my_tool import handle_my_tool"

# Functional test
cd backend && venv/bin/python -m pytest tests/test_my_tool.py -p no:jaxtyping -p no:cacheprovider
```

### 27.2 Adding a New Skill

Skills are more complex than tools — they include a methodology prompt, a set of injected tools, and optionally a sandbox execution step.

**Step 1: Create the skill directory**

```
backend/app/skills/my_skill/
├── skill.md           # Methodology prompt (required)
├── tools.py           # Skill-specific tool definitions (optional)
├── handler.py         # Skill-specific tool handlers (optional)
└── config.json        # Skill metadata (optional)
```

**Step 2: Write the methodology prompt** (`skill.md`)

```markdown
# My Skill Methodology

## Purpose
What this skill does and when to use it.

## Step-by-Step Process
1. First, gather information using tool_name
2. Analyze the results
3. Generate the output using tool_name

## Output Format
Describe the expected output format.

## Constraints
- What the skill will NOT do
- Edge cases to handle
```

**Step 3: Define skill tools** (if the skill needs custom tools)

In `tools.py`:

```python
MY_SKILL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "skill_my_tool",
            "description": "Tool specific to this skill",
            "parameters": { ... }
        }
    }
]
```

**Step 4: Register the skill**

Skills are auto-discovered from `backend/app/skills/`. The skill loader scans subdirectories for `skill.md` files and registers them in the skill catalog.

**Step 5: Test the skill**

1. Create an agent with the skill enabled (via Agent Builder or API)
2. Send a message that triggers the skill
3. Verify the skill methodology is injected into the system prompt
4. Verify skill-specific tools are available

### 27.3 Tool vs Skill Decision Matrix

| Criterion | Tool | Skill |
|-----------|------|-------|
| Single operation | Yes | No |
| Multi-step workflow | No | Yes |
| Needs methodology prompt | No | Yes |
| Has its own tools | No | Yes (injected when active) |
| Sandbox execution | Optional | Common |
| Marketplace listing | No | Yes |

---

## 28. Error Propagation Map

### 28.1 Two-Layer Retry Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Tool Call Execution                        │
│                                                              │
│  Layer 1: Handler-level retry (tool_retry.py)                │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ retry_with_backoff(handler, max_retries=3)            │    │
│  │   Transient errors → exponential backoff retry        │    │
│  │   Permanent errors → {"success": False, "error": ...} │    │
│  │   Exhausted → {"failure_kind": "transient_exhausted"}│    │
│  └──────────────────────┬───────────────────────────────┘    │
│                         │                                    │
│  Layer 2: Result-level retry (agent_tools.py)                │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ execute_tool_with_retry()                             │    │
│  │   Checks _is_retryable(result) — string matching     │    │
│  │   Anti-double-retry: skips if failure_kind already set│    │
│  │   If TOOL_ARG_REFORMULATION_ENABLED:                  │    │
│  │     Asks LLM to reformulate args → re-execute once   │    │
│  └──────────────────────┬───────────────────────────────┘    │
│                         │                                    │
│                         ▼                                    │
│  Result: {"success": True/False, "error": ..., "result": ...}│
└─────────────────────────────────────────────────────────────┘
```

### 28.2 Error Classification

| Category | Examples | Action |
|----------|----------|--------|
| **Transient** | `asyncio.TimeoutError`, `ConnectionError`, `ConnectionResetError` | Retry with backoff |
| **API transient** | "connect", "timeout", "refused" in error text | Retry with backoff |
| **Permanent** | "permission denied", "unknown tool", "not found" | Return error immediately |
| **Reformable** | Tool arg format errors, missing optional args | LLM reformulation + 1 retry |

### 28.3 SSE Error Events to Frontend

```
Backend Error                    SSE Event                    Frontend Display
─────────────                    ─────────                    ────────────────
Agent loop exception     →   {"type":"error",           →   Red error banner
   (BaseException)            "message":"Agent loop      →   in chat
                               failed: {exc}"}

LLM API error            →   {"type":"error",           →   Error message
   (rate limit, timeout)      "message":"LLM API error"}  →   inline

Tool execution error     →   (tool_result with           →   Tool step shows
                              success:false)              →   error + retry

Stream interrupted       →   (no "done" event)           →   Spinner stuck
                                                          →   (BUG: no timeout)
```

### 28.4 Parallel Tool Execution Error Isolation

```python
# agents.py — sibling tool calls use return_exceptions=True
results = await asyncio.gather(*tasks, return_exceptions=True)
# One sibling failure does NOT cancel others
# Exceptions converted to: {"success": False, "error": f"{type(r).__name__}: {r}"}
```

### 28.5 Reflexion on Tool Failures

After each iteration, `_collect_tool_failures` (agents.py:4175-4190) builds a reflexion prompt:
- Lists all failed tool calls with their error messages
- Injects back into LLM context as a "tool failure summary" message
- LLM can self-correct by adjusting arguments or using alternative tools

---

## 29. Production Runbook

### 29.1 Common Failure Modes

| Symptom | Root Cause | Diagnosis | Remediation |
|---------|-----------|-----------|-------------|
| **Spinner stuck forever** | SSE stream ended without `done` event | Check backend logs for unhandled exception | Restart backend; add frontend watchdog timeout |
| **"Querying the {data_source_id}"** | LLM omitted `data_source_id` arg | Check LLM function call args in trace | Auto-resolve from `bound_kb_ids` when exactly 1 KB |
| **"TypeError: unexpected keyword"** | `execute_tool()` signature mismatch | Check handler function signature vs call site | Align signatures; regression test |
| **Circuit breaker open** | 3+ consecutive LLM provider failures | `provider_health.py` logs | Wait 30s cooldown or switch to fallback provider |
| **DB pool exhaustion** | Too many concurrent queries | `psycopg2.InterfaceError` in logs | Reduce MAX_TOOL_ITERATIONS; add pool size config |
| **Tool call 400 loop** | `tool_calls` not stripped from messages | Check `pre_call_prep` is called | Ensure `_sanitize_messages_for_no_tools` runs |
| **Empty assistant message** | Sub-loop returned no data, synthesis skipped | Check `last_rows is None` path | Add fallback synthesis when `last_rows is None` |
| **Orphan container** | Sandbox timeout but rm -f failed | `docker ps -a \| grep sandbox` | `docker rm -f <container_id>` |
| **Double logging** | Handler registered twice | Two lines with same format | Check `logging.getLogger("zhanlu")` setup |

### 29.2 Critical Metrics to Monitor

| Metric | Alert Threshold | Source |
|--------|----------------|--------|
| LLM call latency P99 | > 30s | `provider_health.py` |
| Tool execution error rate | > 20% | Agent loop metrics |
| Active DB connections | > 80% of pool | `psycopg2` pool stats |
| Sandbox container count | > 50 concurrent | `docker ps` |
| Redis queue depth | > 100 pending jobs | `LLEN sandbox:queue` |
| SSE stream duration | > 5 min | Frontend timeout |
| Circuit breaker opens | > 0 per hour | Provider health logs |

### 29.3 Emergency Procedures

**Backend unresponsive:**
```bash
docker restart zhanlu-backend zhanlu-sandbox-worker
# Wait 30s, then:
curl localhost:5002/healthz
```

**Database migration needed:**
```bash
docker exec zhanlu-backend alembic upgrade head
docker restart zhanlu-backend
```

**Stale frontend build:**
```bash
cd frontend && ./node_modules/.bin/vite build --emptyOutDir=false
docker restart zhanlu-frontend
```

**Orphan sandbox containers:**
```bash
docker ps -a --filter "name=sandbox-" --format "{{.ID}}" | xargs docker rm -f
```

**Redis queue stuck:**
```bash
docker exec zhanlu-redis redis-cli LLEN sandbox:queue
# If > 0 and no worker processing:
docker restart zhanlu-sandbox-worker
```

---

## 30. Database Migration Workflow

### 30.1 Alembic Setup

**Directory**: `backend/migrations/` (or `backend/alembic/`)

Key files:
- `alembic.ini` — DB connection string, script location
- `env.py` — Imports all SQLAlchemy models from `app.models`, configures `target_metadata`
- `versions/` — Migration files numbered sequentially (e.g., `001_initial.py`, `011_add_tools_is_default.py`)

### 30.2 Creating a New Migration

```bash
# 1. Create a new model in backend/app/models/
# 2. Import it in backend/app/models/__init__.py so Alembic detects it
# 3. Generate the migration:
docker exec zhanlu-backend alembic revision --autogenerate -m "add_my_new_table"

# 4. Review the generated migration in backend/migrations/versions/
# 5. Apply:
docker exec zhanlu-backend alembic upgrade head
```

### 30.3 Tenant Column Injection

Every model that needs multi-tenant isolation includes:

```python
class MyModel(Base):
    __tablename__ = "my_table"
    
    org_id = Column(String, nullable=False, index=True)  # Tenant isolation
    app_id = Column(String, nullable=False, default="default-app")  # App scope
```

The `_apply_tenant(query, org_id, app_id)` filter is applied to ALL queries that should be tenant-scoped. Missing this is a **security vulnerability**.

### 30.4 Auto-Migration on Startup

The backend `prestart.sh` runs:
```bash
alembic upgrade head
```

This means:
- New migrations are applied automatically on container restart
- Failed migrations will block backend startup
- Always test migrations locally before deploying

### 30.5 Migration Gotchas

- **`coerceStringArray`**: JSON columns (`capabilities`, `skills`, `knowledge_bases`, `sub_agents`) must use the `coerceStringArray` helper to normalize both `["a","b"]` and `"a,b"` formats
- **Index on `org_id`**: Always add an index on `org_id` for tenant-scoped queries
- **Nullable vs default**: Use `nullable=False, default="default-app"` for `app_id` to avoid NULL issues
- **JSON columns in SQLite**: Alembic may generate incompatible SQL for SQLite; test with Postgres

---

## 31. A2A Protocol & Swarm Architecture

### 31.1 Google Agent-to-Agent (A2A) Protocol

Zhanlu implements a JSON-RPC-based A2A protocol for inter-agent communication, following the Google A2A specification.

**Core concepts**:
- **Agent Card** — JSON manifest describing an agent's capabilities, skills, and endpoints
- **Task** — A unit of work sent from one agent to another
- **Message** — Communication within a task (user → agent, agent → user)
- **Part** — Content within a message (text, file, data)

**Protocol flow**:
```
Agent A                          Agent B
  │                                │
  │── POST /a2a/tasks/send ──────▶│  (create task)
  │◀── 200 {task: {id, ...}} ─────│
  │                                │
  │── POST /a2a/tasks/{id} ──────▶│  (get task status)
  │◀── 200 {task: {status, ...}} ─│
  │                                │
  │── POST /a2a/tasks/{id}/       │
  │         cancel ──────────────▶│  (cancel task)
  │◀── 200 {task: {status:...}} ──│
```

### 31.2 Current A2A Implementation Status

The A2A protocol has a **full implementation** in `backend/app/services/a2a/`:

| Module | File | Status |
|--------|------|--------|
| Agent Card | `a2a/agent_card.py` | **Implemented** — `build_agent_card()` generates A2A v1.0 AgentCard JSON from AgentApp |
| Task Store | `a2a/tasks.py` | **Implemented** — `A2aTaskStore` (CRUD for A2aTask rows), `A2aExecutor` protocol, `DefaultA2aExecutor` |
| Remote Client | `a2a/client.py` | **Implemented** — JSON-RPC 2.0 client to delegate tasks to external A2A agents |
| Router | `routers/a2a.py` | **Implemented** — `POST /a2a` (JSON-RPC: tasks/send, tasks/get, tasks/cancel), `GET /a2a/agent-card` |
| DB Model | `models/a2a_task.py` | **Implemented** — `A2aTask` with status lifecycle, artifacts, messages |
| Tests | `tests/test_a2a_protocol.py` | **Implemented** — agent card, task send/get/cancel, remote client |

**What's NOT yet implemented**:
- Push notifications (streaming task updates to external agents)
- Artifact streaming (large artifact delivery over A2A)
- Multi-turn task collaboration (back-and-forth negotiation)

### 31.3 Swarm Architecture (Roadmap)

The swarm/ multi-agent orchestration is currently a **stub** — the directory exists but contains no active implementation. The intended architecture:

```
┌─────────────────────────────────────────────────────────┐
│                 Swarm Orchestrator (Planned)              │
│                                                         │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐          │
│  │ Agent A   │  │ Agent B   │  │ Agent C   │          │
│  │ (Research)│  │ (Analysis)│  │ (Writing) │          │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘          │
│        │              │              │                  │
│        ▼              ▼              ▼                  │
│  ┌──────────────────────────────────────────────┐      │
│  │           Shared Task Queue (Redis)           │      │
│  └──────────────────────────────────────────────┘      │
│        │                                                │
│        ▼                                                │
│  ┌──────────────────────────────────────────────┐      │
│  │         Swarm Orchestrator                    │      │
│  │  - Task decomposition (DAG)                  │      │
│  │  - Parallel agent dispatch                   │      │
│  │  - Result aggregation                        │      │
│  │  - Fork/join synchronization                 │      │
│  └──────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────┘
```

**Planned features**:
- **Fork/join** — Split a task into sub-tasks, dispatch to multiple agents, collect results
- **DAG execution** — Topological sort for parallel sub-task execution (code exists but unused)
- **Agent specialization** — Route sub-tasks to agents by capability matching
- **Shared memory** — Agents can read/write to a shared context store
- **A2A interop** — Standard A2A protocol for cross-platform agent communication

### 31.4 Market Context — Why Agent Harness Is the Hottest Topic

The AI engineering market in 2026 is defined by the transition from **single-agent chatbots** to **multi-agent orchestrator platforms**. Key trends:

| Trend | Market Leaders | Zhanlu Status |
|-------|---------------|---------------|
| **Agent SDK** (structured loops) | Anthropic Agent SDK, LangGraph | ✅ Harness orchestrator exists |
| **Context engineering** (prompt layering, compaction) | Claude context editing, Kimi 300K | ✅ 5-layer constitutional + 8-layer dynamic |
| **Tool-use reliability** (retry, reformulation, self-healing) | ReAct + reflection papers | ✅ Two-layer retry + LLM reformulation |
| **Multi-agent swarm** (parallel, fork/join) | Kimi K2.6 300-agent, MiniMax M2 | ❌ Stub — top priority |
| **Agent-to-Agent protocol** | Google A2A, MCP | 🔨 Partial implementation |
| **Checkpoint/resume** (long-running tasks) | CrewAI, AutoGen | ✅ messages_snapshot recovery |
| **Budget enforcement** (token/cost limits) | Token-level routing | ✅ Three-layer clamping + tier routing |
| **Sub-agent reasoning passback** | Claude subagents, Gemini | ✅ Implemented in data agent |

**Zhanlu's competitive moat**:
1. **GATE policy evaluation** — runtime access control before every tool call
2. **3-layer compaction engine** — sophisticated context compression (vs naive truncation)
3. **Self-healing mapper** — retry with alternative prompts, pattern fallback
4. **Checkpoint/resume** — crash recovery from messages_snapshot
5. **Sandbox isolation** — DooD with LLM proxy, comprehensive security flags
6. **Constitutional prompts** — 5-layer + 8-layer injection = industry-leading prompt engineering surface

**Top gaps to close** (priority order):
1. **Parallel DAG execution** — topological sort exists but nodes run serially
2. **VERIFY replan loop** — currently a no-op logger
3. **Agent-node executor** — `capability_router` "agent" type falls through to tool
4. **Swarm orchestration** — fork/join, shared memory, capability-based routing
5. **A2A full implementation** — push notifications, artifact streaming, multi-turn

---

### C.16 Agent Harness — Orchestrator Execution Flow

```python
async def run(self) -> RunResult:
    """AgentRunOrchestrator.run() — exact execution flow."""
    
    # ─── INITIALIZATION ───
    self._emit("run_start", {"run_id": self.run_id, "task": self.task[:200]})
    guard_ctrl = ToolLoopGuardController(per_tool_caps=self.per_tool_caps)
    budget = IterationBudget(max_iterations=self.max_iterations)
    messages = [
        {"role": "system", "content": self.system_prompt},
        {"role": "user", "content": self.task},
    ]
    
    # ─── MAIN LOOP ───
    for iteration in range(self.max_iterations):
        budget.tick()
        
        # Pre-call prep (message sanitization)
        if self.pre_call_prep:
            messages = await self.pre_call_prep(messages)
        
        # LLM call
        try:
            llm_result = await self.llm_fn(
                messages=messages,
                tools=self.tool_schemas,  # None on forced synthesis
                temperature=self.temperature,
            )
        except Exception as exc:
            return RunResult(success=False, error=f"LLM call failed: {exc}", iterations=iteration)
        
        self._emit("llm_call", {"iteration": iteration, "duration_ms": ..., "tokens": ...})
        
        # No tool calls → final answer
        content = llm_result.get("content", "")
        tool_calls = llm_result.get("tool_calls", [])
        
        if not tool_calls:
            final_text = content
            break
        
        # Append assistant message
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        
        # Execute each tool call
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            tool_args = tc["function"]["arguments"]
            
            # Guard checks
            if tool_name in self.denied_tools:
                tool_result = {"success": False, "error": f"Tool '{tool_name}' is denied"}
            elif tool_name not in self.allowed_tools:
                tool_result = {"success": False, "error": f"Tool '{tool_name}' not in allowed list"}
            else:
                # Dispatch to handler
                tool_result = await self.tool_dispatcher(
                    tool_name=tool_name,
                    args=tool_args,
                    db=self.db,
                    user_id=self.user_id,
                    context=self.context,
                )
            
            # Guard controller update
            guard_ctrl.after_call(tool_name)
            if guard_ctrl.should_halt:
                final_text = "Tool call budget exhausted"
                break
            
            # Append tool result
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_result),
            })
        
        if guard_ctrl.should_halt:
            break
        
        # Turn budget enforcement
        if self.apply_turn_budget:
            await self.apply_turn_budget(iteration, budget.remaining)
    
    # ─── FORCED SYNTHESIS ───
    if not final_text:
        synthesis_result = await self.llm_fn(
            messages=messages,
            tools=None,  # No tools → forced text response
            temperature=0.3,
        )
        final_text = synthesis_result.get("content", "")
    
    # ─── FINALIZE ───
    return self._finish(
        RunResult(
            success=True,
            answer=final_text,
            tool_calls=self._tool_calls_log,
            iterations=iteration + 1,
        )
    )
```

---

### C.17 LLM Request Pipeline — Cost-Aware Model Selection

```python
async def _call_llm_cost_aware(messages, tools, temperature, task_type=None):
    """Complete LLM request pipeline with cost-aware model selection."""
    
    # 1. Classify task type
    if task_type is None:
        task_type = classify_task(messages, tools_specified=bool(tools))
    # Returns: "simple_chat" | "reasoning" | "tool_use" | "code_gen" | "document_gen"
    
    # 2. Route to model
    model = route_model(task_type)  # Falls back to settings.LLM_MODEL
    
    # 3. Health routing (if enabled)
    if settings.LLM_HEALTH_ROUTING_ENABLED:
        providers = get_llm_providers()
        provider = select_provider(providers)  # Circuit-breaker aware
        base_url = provider.base_url
        api_key = provider.api_key
    
    # 4. Estimate tokens and clamp max_tokens
    estimated_input = estimate_messages_tokens(messages)
    max_tokens = _clamp_max_tokens_for_context(
        max_tokens=settings.LLM_MAX_TOKENS_HARD_CAP,
        endpoint=model,
        messages=messages,
        tools=tools,
    )
    # Three layers: per-model ceiling → dynamic headroom → global hard cap
    
    # 5. Fixed-temperature model check
    if model in settings.LLM_FIXED_TEMPERATURE_MODELS.split(","):
        temperature = None  # o1/o3/kimi-k2 reject custom temperature
    
    # 6. Make the API call
    response = await chat_completion(
        messages=messages,
        model=model,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        base_url=base_url,
        api_key=api_key,
    )
    
    # 7. Record health metrics
    record_provider_latency(provider_name, duration_ms)
    
    return response
```

---

### C.18 Sandbox Container Lifecycle — Exact Sequence

```
1. Backend receives tool call requiring sandbox
   │
2. Backend enqueues job → Redis "sandbox:queue"
   │  Job payload: {job_id, image_type, input_dir, command, timeout, memory, cpus}
   │
3. Sandbox worker BRPOP from Redis queue
   │
4. Worker resolves image: image_type → IMAGE_MAP → docker image name
   │  "pptx" → zhanlu-sandbox-pptx:latest
   │  "skill_runner" → zhanlu-sandbox-skill:latest
   │
5. Worker resolves mount paths (DooD translation):
   │  docker inspect $HOSTNAME → get container-local paths
   │  Translate to host paths for volume mounting
   │
6. Worker spawns container:
   │  docker run --rm --network none --read-only --memory 1g --cpus 1
   │            --cap-drop ALL --tmpfs /tmp:rw,size=256m
   │            -v {input}:/input:ro -v {output}:/output:rw
   │            {image} {command}
   │
7. Container executes in isolation:
   │  - Reads from /input (read-only)
   │  - Writes to /output (read-write)
   │  - No network access
   │  - LLM calls via Unix socket proxy (skill_runner only)
   │
8a. Normal exit:
   │  - Worker reads /output for results
   │  - Container auto-removed (--rm flag)
   │  - Job status → "completed"
   │
8b. Timeout (default 120s):
   │  - subprocess.TimeoutExpired raised
   │  - Worker runs: docker rm -f {container_name}
   │  - Job status → "failed" with timeout error
   │
9. Worker posts result back:
   │  - Updates SandboxJob row in DB
   │  - Backend polls DB or receives Redis event
   │  - Tool call returns with sandbox output
```

---

## 32. Frontend Chat State Machine

### 32.1 State Variables

The Chat component (`frontend/src/pages/Chat.jsx`) manages a complex set of states:

| Variable | Initial | Purpose |
|----------|---------|---------|
| `messages` | `[]` | All chat messages (user + assistant), each with streaming metadata |
| `loading` | `false` | True while a send is in-flight (prevents duplicate sends) |
| `streamingId` | `null` | ID of currently-streaming assistant message (`null` = idle) |
| `streamingConvId` | `null` | Conversation ID of the in-flight v3 stream |
| `inputValue` | `localStorage draft` or `''` | Chat input text |
| `activeSkill` | `null` | Currently selected skill |
| `activeAgent` | `null` | Currently selected agent |
| `canvasArtifact` | `null` | Editable HTML artifact |
| `attachments` | `[]` | File attachments for next send |
| `feedbackByMessage` | `{}` | Per-message thumbs up/down |
| `loadStatus` | `'idle'` | `'idle' | 'loading' | 'loaded' | 'error'` |

**Derived state** (computed at render time):
```javascript
const chatInputBusy = stream.isActiveForSession(activeId) || isRunInFlight(latestExecution);
const idle = !streamingId && !loading;
const isStreaming = m.id === streamingId;  // per message
const canRegenerate = m.role === 'assistant' && !!m.content && m.id === lastId && idle && !isStreaming;
```

### 32.2 Chat State Diagram

```
                    ┌──────────┐
                    │   IDLE   │  (streamingId=null, loading=false)
                    └────┬─────┘
                         │ handleAgentSend()
                         │ sets: loading=true, streamingId=msgId
                         ▼
                    ┌──────────┐
              ┌────▶│STREAMING │  (streamingId set, loading=true)
              │     └────┬─────┘
              │          │ SSE events: delta, tool_progress, phase, plan_step
              │          │   → updates message object in-place
              │          │
              │     ┌────┴────────────┐
              │     │                 │
              │     ▼                 ▼
              │  ┌──────────┐   ┌──────────────┐
              │  │TOOL PROG │   │  REFINING    │
              │  │(activity │   │ (content_    │
              │  │ steps    │   │  preserve)   │
              │  │ visible) │   │  then replace│
              │  └────┬─────┘   └──────┬───────┘
              │       │                │
              │       └───────┬────────┘
              │               │ more delta events
              │               │
              │               ▼
              │          ┌──────────┐
              │          │  PAUSED  │  (SSE "paused" event)
              │          │ (requires│  → shows "Continue" button
              │          │  confirm)│  → user clicks → sends "continue"
              │          └────┬─────┘
              │               │ continue message sent
              │               ▼
              │          ┌──────────┐
              └──────────│STREAMING │  (new SSE stream for continuation)
                         └────┬─────┘
                              │ SSE "done" event
                              │ sets: streamingId=null, loading=false
                              ▼
                         ┌──────────┐
                         │  IDLE    │  (message finalized with content)
                         │          │  → canRegenerate=true on last msg
                         └──────────┘
```

### 32.3 Per-Message Streaming Metadata

These are properties on the message object (NOT separate useState), mutated via `setMessages`:

| Property | Set By SSE Event | Purpose |
|----------|-----------------|---------|
| `content` | `delta`, `content_replace`, `done` | Accumulated assistant text |
| `loading` | set on creation | True while streaming |
| `fsm_state` | `fsm_state` | FSM cognitive state (init/goal/plan/act/verify) |
| `tool_calls` | `tool_progress`, `done` | Tool call array with status |
| `activity_steps` | `activity_step` | Numbered step indicators |
| `live_events` | `live_event` | Typed live-activity feed events |
| `phase` | `phase` | Phase headline `{state, verb, title}` |
| `streaming_reasoning` | `reasoning_delta` | Per-token reasoning for live one-liner |
| `streaming_plan_steps` | `plan_step_added/completed` | Live todo checklist |
| `streaming_action` | `tool_progress` | Tool-action text for live one-liner |
| `refining` | `content_preserve` (true), `content_replace`/`done` | Re-generation in progress |

### 32.4 SSE Event → State Update Mapping

**File**: `frontend/src/api/agentEnhanced.js` — `streamAgentResponse()`

```javascript
SSE Event           │ Action
────────────────────┼──────────────────────────────────────────
delta               │ Append text to message.content
content_replace     │ Replace entire message.content
content_preserve    │ Set message.refining = true
tool_progress       │ Push to message.tool_calls[] + message.activity_steps[]
activity_step       │ Push numbered step indicator
phase               │ Set message.phase = {state, verb, title}
plan_step_added     │ Push to message.streaming_plan_steps[]
plan_step_completed │ Mark step completed in streaming_plan_steps
reasoning_delta     │ Append to message.streaming_reasoning
live_event          │ Push to message.live_events[]
fsm_state           │ Set message.fsm_state
done                │ Set streamingId=null, loading=false, message.loading=false
paused              │ Set message.paused=true (shows Continue button)
error               │ Display error banner, set streamingId=null
```

### 32.5 Known Frontend Bugs

| Bug | Impact | Status |
|-----|--------|--------|
| **No stream timeout** — `streamAgentResponse` has no read timeout | If SSE stream ends without `done`, spinner stuck forever | Open |
| **Silent stream end** — stream closure without `done` persists empty assistant message | User sees blank message with no error | Open |
| **Activity steps "running" forever** — `handleAgentSend` leaves steps as "running" if stream ends without `done` | UI shows phantom in-progress steps | Open |

---

## 33. GATE Security Model

### 33.1 Four-Layer Authorization Stack

Zhanlu implements defense-in-depth with four authorization layers that run **before any tool executes**:

```
┌─────────────────────────────────────────────────────────────────┐
│                  Tool Call Authorization Stack                   │
│                                                                  │
│  Layer 1: RBAC Tool Filtering (before LLM sees tools)           │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ tool_registry.get_definitions() calls authz.filter()      │  │
│  │ Denied tools are STRIPPED from the schema → LLM can't    │  │
│  │ even attempt to call them                                 │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Layer 2: GATE Policy Evaluation (before plan execution)         │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ policy_evaluator.evaluate_plan() → allow/deny/confirm     │  │
│  │ Runs BEFORE any tools execute. Sandbox nodes → confirm.   │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Layer 3: ACT Node-Level Check (before each node)                │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ policy_evaluator.evaluate_node() → allow/deny             │  │
│  │ Per-tool risk assessment. nl2sql = always allow.           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Layer 4: Denied-Tools Runtime Check (in orchestrator)           │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ orchestrator checks tool_name against denied_tools set     │  │
│  │ Also checks allowed_tools whitelist                        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Only after ALL FOUR layers pass → tool handler executes        │
└─────────────────────────────────────────────────────────────────┘
```

### 33.2 GATE Policy Evaluator

**File**: `backend/app/services/synexia/policy_evaluator.py`

```python
def evaluate_plan(plan, task_spec: dict, agent_name: str) -> dict:
    """GATE-level (whole-plan) check. Runs BEFORE any execution.
    
    Returns: allow | deny | require_confirm
    Risk tiers: low | medium | high
    """
    # Assess risk:
    # - has_sandbox → medium
    # - configure_system → medium  
    # - requires_confirmation → high
    # - nl2sql only → low (read-only)
    
    # Apply default policy pack rules
    # Return decision with risk_tier and reasons

def evaluate_node(node, policy_decision: dict) -> dict:
    """ACT-level (per-node) check. Runs before each node executes."""
    # If plan was denied → all nodes denied
    # nl2sql → always allow
    # sandbox + plan_approved → allow
    # sandbox + plan_not_approved → deny
```

### 33.3 Default Policy Pack

**File**: `backend/policies/default.yaml`

```yaml
rules:
  - id: allow_low_risk
    condition: "risk_tier == 'low'"
    decision: allow
  - id: confirm_medium_risk
    condition: "risk_tier == 'medium'"
    decision: require_confirm
  - id: deny_high_risk_without_approval
    condition: "risk_tier == 'high' and not has_approval"
    decision: deny
  - id: sandbox_elevates_risk
    condition: "has_sandbox and risk_tier == 'low'"
    decision: require_confirm

node_rules:
  - condition: "node_type == 'nl2sql'"
    decision: allow              # Read-only data queries always allowed
  - condition: "node_type == 'sandbox' and plan_approved"
    decision: allow              # Sandbox approved at gate
  - condition: "node_type == 'sandbox' and not plan_approved"
    decision: deny               # Sandbox requires plan approval first
```

### 33.4 RBAC Role Definitions

**File**: `backend/app/services/authz/rbac.py`

```python
_VIEWER_ALLOWED_TOOLS = ["read_file", "web_search", "web_extract",
    "list_tools", "list_knowledge_bases", "list_market_agents"]

BUILTIN_ROLES = {
    "admin": {"tools": {"allow": None, "deny": []}},    # None = all allowed
    "user":  {"tools": {"allow": None, "deny": []}},    # All by default
    "viewer": {"tools": {"allow": _VIEWER_ALLOWED_TOOLS, "deny": []}},
}
```

### 33.5 Governance & Audit Trail

**File**: `backend/app/models/governance.py`

Three append-only models for compliance:

| Model | Purpose | Key Fields |
|-------|---------|------------|
| `PolicyDecision` | Every allow/deny/confirm decision | `decision_level` (gate/node), `risk_tier`, `reasons`, `policy_pack` |
| `ApprovalRequest` | Pending user confirmations | `action_type` (publish_artifact, sandbox_exec, external_send, code_skill) |
| `AuditLog` | Never-deleted audit trail | `actor_type` (user/agent/system), `action`, `resource`, `result` |

### 33.6 Data Source Access Control

Data source access is controlled at two levels:
1. **Binding level** — `bound_kb_ids` in the agent's context determines which knowledge bases the data agent can query
2. **Tenant level** — `_apply_tenant(query, org_id, app_id)` ensures cross-tenant data isolation

---

## 34. Persistent Memory & Learning Graph

### 34.1 Three-Layer Memory Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Persistent Memory Architecture                   │
│                                                              │
│  Layer 1: AgentMemory (DB-persisted, per-agent + per-user)  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ target="memory" — Project-scoped environment facts     │  │
│  │ target="user"   — Cross-project user knowledge         │  │
│  │ Pinned memories always in context (importance=∞)       │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  Layer 2: Learning Graph (JSON file, per-agent)              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Tracks successful techniques and failure warnings       │  │
│  │ Confidence increases with repetitions (max 1.0)         │  │
│  │ Injected as "Learned from past sessions" block          │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  Layer 3: Semantic Retrieval (embedding-based recall)        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ AgentMemory rows with embeddings → cosine similarity    │  │
│  │ Top-K memories by relevance injected into prompt        │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  All three layers are loaded ONCE at conversation start      │
│  via load_memory_snapshot() → injected into system prompt    │
└─────────────────────────────────────────────────────────────┘
```

### 34.2 AgentMemory DB Model

**File**: `backend/app/models/agent_memory.py`

```python
class AgentMemory(TimestampedBase):
    __tablename__ = "agent_memories"
    
    agent_app_id: Mapped[str]          # Which agent
    user_id: Mapped[str | None]        # Which user
    project_id: Mapped[str | None]     # Project scope
    target: Mapped[str]                # "memory" or "user"
    content: Mapped[str]               # §-delimited entries
    char_count: Mapped[int]
    content_hash: Mapped[str | None]   # SHA-256 dedup
    importance: Mapped[int]            # 0 default
    ttl_days: Mapped[int | None]       # Optional TTL
    usage_count: Mapped[int]           # Incremented on recall
    pinned: Mapped[bool]               # Always-in-context
    embedding: Mapped[list | None]     # For semantic recall
```

Two targets:
- `target="memory"` — Agent's project-scoped notes (environment facts, tool quirks). Strictly project-scoped to prevent cross-project leakage.
- `target="user"` — What the agent knows about the user (name, role, preferences). Always cross-project.

### 34.3 Learning Graph

**File**: `backend/app/services/learning_graph.py`

```python
@dataclass
class Learning:
    technique: str       # What was done (e.g. "used pytest with -v flag")
    outcome: str         # "success" or "failure"
    context: str         # What problem it solved/failed on
    confidence: float    # 0-1, increases with repetitions
    timestamp: str       # ISO datetime
    tool: str = ""       # Which tool was involved
```

Storage: **One JSON file per agent** at `$ZHANLU_DATA_DIR/learning_graphs/{agent_app_id}.json`. No database table.

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `_MAX_LEARNINGS` | 50 | Max entries per agent (pruned by confidence) |
| `_MIN_PROMPT_CONFIDENCE` | 0.4 | Min confidence to appear in system prompt |

Prompt injection format:
```
[Learned from past sessions]
Techniques that worked well:
  - use pytest -v (for: debugging)
Approaches to avoid:
  - avoid rm -rf (failed on: destructive)
```

### 34.4 Memory Snapshot Loading

**File**: `backend/app/services/tool_handlers/memory_tool.py`

```python
def load_memory_snapshot(db, agent_app_id, user_id, project_id=None) -> dict[str, str]:
    """Called once at conversation start. Returns {"memory": "...", "user": "..."}."""
    
    # target='memory' rows are STRICTLY project-scoped (no NULL fallback)
    # Prevents "Q2 2026 sales report leak" — notes from one project can't
    # bleed into another
    
    # target='user' rows are cross-project (user preferences apply everywhere)
```

### 34.5 Entity Masters & Proven Recipes

**Entity Masters** (`dynamic_prompt_builder.py` — `_inject_entity_masters`):
- Domain entity dictionary (e.g., C5/C9 products, ERP table-column mappings)
- Injected as structured context for NL2SQL accuracy

**Proven Recipes** (`dynamic_prompt_builder.py` — `_inject_recipes`):
- Previously successful answer patterns stored in `AgentMemory`
- Injected as few-shot examples: "For question type X, use approach Y"

---

## 35. Artifact Generation Pipeline

### 35.1 Two-Tool Lifecycle

**File**: `backend/app/services/tool_handlers/artifact_tool.py`

```
┌─────────────────────────────────────────────────────────────┐
│               Artifact Generation Pipeline                    │
│                                                              │
│  create_artifact:                                            │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 1. Create Artifact row (draft)                       │    │
│  │ 2. Create Version row (building)                     │    │
│  │ 3. Render file bytes via format exporter             │    │
│  │ 4. Store original blob → MinIO                       │    │
│  │ 5. Run preview_builder.convert_to_preview()          │    │
│  │ 6. Store preview blob → MinIO                        │    │
│  │ 7. Mark version as preview_ready                     │    │
│  │ 8. Return {artifact_id, version, file_url, ...}      │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                              │
│  edit_artifact:                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 1. Look up artifact + current version's source_json  │    │
│  │ 2. Call LLM to interpret edit instructions           │    │
│  │ 3. Create new version with modified payload          │    │
│  │ 4. Re-render file bytes + store blob                 │    │
│  │ 5. Return updated artifact info (version bumps)      │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### 35.2 Supported Artifact Types

| Type | MIME | Exporter | Notes |
|------|------|----------|-------|
| `docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | python-docx | Word document |
| `pdf` | `application/pdf` | reportlab | PDF document |
| `pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | python-pptx | PowerPoint |
| `xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | openpyxl | Excel |
| `html` | `text/html` | Jinja2 template | Web page |

### 35.3 PPTX Professional Pipeline

When `PPT_DECK_PLANNER_ENABLED` + `PPT_CREATE_ARTIFACT_PIPELINE_ENABLED` are both true, PPTX goes through the professional pipeline:

```
1. Deck Planner (LLM) → structured slide plan
2. Slide Router → assigns layout type per slide
3. Layout Engine → renders each slide
4. Audit/Repair/Polish → validates and fixes
5. ExportService.render_pptx_deck() → final .pptx bytes
```

### 35.4 Layout Engine — 12 Layout Types

**File**: `backend/app/services/artifacts/layout_engine.py`

| Layout | Description |
|--------|-------------|
| `cover` | Title slide with subtitle and date |
| `agenda` | Numbered agenda items |
| `kpi_grid` | 2×2 or 3×2 metric cards |
| `chart_full` | Full-width chart |
| `chart_with_bullets` | Chart + bullet points |
| `findings_cards` | Key finding cards |
| `insights_bullets` | Insight bullet points |
| `recommendations` | Numbered recommendation blocks |
| `data_table` | Formatted data table |
| `methodology` | Methodology description |
| `section_break` | Section divider |
| `thank_you` | Closing slide |

### 35.5 Artifact Versioning

Each artifact has multiple versions:
- Version 1 created by `create_artifact`
- Subsequent versions created by `edit_artifact`
- Each version stores: `source_json` (editable payload), `file_url` (rendered blob), `preview_url` (preview blob), `status` (building → ready → preview_ready)
- Version number auto-increments on each edit

---

## 36. SSE/Stream Debugging Guide

### 36.1 End-to-End Request Trace

```
Frontend (Chat.jsx)                Backend (agents.py)              External
──────────────────                ─────────────────                ────────
                                  
1. handleAgentSend()              
   │ sets: loading=true,          
   │   streamingId=msgId          
   │                              
2. streamAgentResponse()          
   │ POST /v3/.../messages/stream 
   │──────────────────────────────▶3. add_message_stream()
   │                               │ Creates assistant message row
   │                               │ Enters v3 loop
   │                               │
   │                          ┌────│ LLM call #1 (system + user msg)
   │                          │    │─────▶ DeepSeek API
   │◀─ SSE: delta ───────────│◀───│◀───── response chunks
   │  (text appended)         │    │
   │                          │    │ Tool call detected
   │                          │    │
   │◀─ SSE: tool_progress ───│    │4. execute_tool_with_retry()
   │  (step indicator)        │    │   ├── Layer 1: handler retry
   │                          │    │   └── Layer 2: result retry
   │                          │    │
   │◀─ SSE: delta ───────────│◀───│ Tool result → LLM call #2
   │  (more text)             │    │
   │                          │    │ ... repeat until no tool calls
   │                          │    │
   │◀─ SSE: done ────────────│◀───│ Final answer emitted
   │  (streamingId=null)      │    │ Agent loop exits
   │                          └────│
   │                              
5. Message finalized              
   canRegenerate=true             
```

### 36.2 Debugging a Stuck Spinner

**Step 1**: Check browser DevTools → Network → find the SSE connection
- If connection is **closed** without `done` → backend crashed or timed out
- If connection is **still open** → backend loop is still running (long tool call)

**Step 2**: Check backend logs
```bash
# Find recent agent loop entries
docker logs zhanlu-backend 2>&1 | grep "agent_loop\|tool_call\|llm_call" | tail -50

# Check for LLM timeout
docker logs zhanlu-backend 2>&1 | grep "timeout\|TimeoutError" | tail -20
```

**Step 3**: Check if tool is stuck
```bash
# Check for hanging sandbox containers
docker ps --filter "name=sandbox-" --format "{{.ID}} {{.RunningFor}}"
```

**Step 4**: Check circuit breaker
```bash
# Check provider health
docker logs zhanlu-backend 2>&1 | grep "circuit_breaker\|provider_health" | tail -10
```

### 36.3 SSE Event Reference

| Event | Payload | Frontend Effect |
|-------|---------|-----------------|
| `delta` | `{"content": "..."}` | Append text to message |
| `content_replace` | `{"content": "..."}` | Replace entire message text |
| `content_preserve` | `{}` | Start refinement mode |
| `tool_progress` | `{"tool_name": "...", "args": {...}, "step": N}` | Add tool step indicator |
| `activity_step` | `{"step": N, "description": "..."}` | Add numbered step |
| `phase` | `{"state": "plan", "verb": "Planning", "title": "..."}` | Phase headline |
| `plan_step_added` | `{"id": "...", "text": "..."}` | Add plan todo |
| `plan_step_completed` | `{"id": "..."}` | Check off plan step |
| `reasoning_delta` | `{"content": "..."}` | Reasoning one-liner |
| `live_event` | `{"type": "...", "data": {...}}` | Typed activity feed |
| `fsm_state` | `{"state": "act"}` | FSM state indicator |
| `paused` | `{"reason": "..."}` | Show Continue button |
| `done` | `{"message_id": "..."}` | Finalize message, go idle |
| `error` | `{"message": "..."}` | Show error banner |

### 36.4 Common Debugging Scenarios

| Scenario | Likely Cause | Fix |
|----------|-------------|-----|
| Spinner stuck, no events | Backend crash before first event | Check backend logs for exception |
| Spinner stuck, last event was `tool_progress` | Tool hanging (sandbox, DB query) | Check sandbox containers / DB connections |
| Empty message after `done` | LLM returned no content | Check LLM response in logs |
| Repeated tool failures | Tool args malformed | Check `_is_retryable` + LLM reformulation |
| `content_preserve` loop | Refinement stuck | Check `content_replace` delivery |
| Stream never starts | `/v3/.../stream` endpoint error | Check route registration, auth token |

---

## 37. File Upload & Processing Pipeline

### 37.1 End-to-End Flow

```
┌─────────────────────────────────────────────────────────────┐
│              File Upload & Processing Pipeline                │
│                                                              │
│  1. Frontend uploads file                                    │
│     POST /api/uploads (multipart/form-data)                  │
│     ┌───────────────────────────────────────────────────┐    │
│     │ file → Backend receives → stores to MinIO          │    │
│     │ returns: {file_url, file_name, file_size, ...}     │    │
│     └───────────────────────────────────────────────────┘    │
│                                                              │
│  2. User attaches file to chat or KB                         │
│     ┌───────────────────────────────────────────────────┐    │
│     │ Chat: file attached to message as attachment       │    │
│     │ KB: file_url stored on KnowledgeBase record        │    │
│     └───────────────────────────────────────────────────┘    │
│                                                              │
│  3. Ingestion pipeline (if KB)                               │
│     ┌───────────────────────────────────────────────────┐    │
│     │ a. File type detection (pdf, docx, txt, csv, etc)  │    │
│     │ b. Text extraction (pdf→text, docx→text, etc)      │    │
│     │ c. Chunking (recursive character splitter)          │    │
│     │ d. Embedding (each chunk → vector)                 │    │
│     │ e. Store chunks in KnowledgeChunk table             │    │
│     └───────────────────────────────────────────────────┘    │
│                                                              │
│  4. Retrieval (at query time)                                │
│     ┌───────────────────────────────────────────────────┐    │
│     │ a. Embed user query                                │    │
│     │ b. Cosine similarity search against chunks         │    │
│     │ c. Return top-K chunks as context                  │    │
│     └───────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### 37.2 Upload Endpoint

**File**: `backend/app/routers/knowledge_base.py`

```python
@router.post("/api/uploads")
async def upload_file(file: UploadFile, ...):
    """Upload file to MinIO, return file_url for attachment."""
    # 1. Validate file type and size
    # 2. Generate unique filename (UUID-based)
    # 3. Store to MinIO via settings.upload_path
    # 4. Return {file_url, file_name, file_size, content_type}
```

### 37.3 Ingestion Pipeline

**File**: `backend/app/services/ingestion/`

The ingestion pipeline processes uploaded files into searchable chunks:

1. **File type detection** — MIME type + extension → selects extractor
2. **Text extraction** — PDF (PyPDF2), DOCX (python-docx), TXT (plain), CSV (pandas), XLSX (openpyxl)
3. **Chunking** — `RecursiveCharacterTextSplitter` with configurable chunk size (default 1000) and overlap (default 200)
4. **Embedding** — Each chunk → embedding model (default `text-embedding-3-small`) → vector stored in `KnowledgeChunk.embedding`
5. **Storage** — `KnowledgeChunk` rows with: `content`, `embedding`, `metadata`, `kb_id`, `char_count`

### 37.4 Reingest Feature

**Endpoint**: `POST /api/knowledge-bases/{kb_id}/reingest`

For Pending/Failed knowledge bases, re-runs ingestion:
- Validates KB is file-based and file exists on disk
- Reconstructs file path from `file_url` + `settings.upload_path`
- Delegates to `run_ingest()` with existing KB record
- Returns `IngestResult` (kb_id, chunk_count, status, error)

### 37.5 Supported File Types

| Type | Extension | Extractor | Ingestion |
|------|-----------|-----------|-----------|
| PDF | `.pdf` | PyPDF2 | Text extraction + chunking |
| Word | `.docx` | python-docx | Text extraction + chunking |
| Text | `.txt`, `.md` | Plain read | Direct chunking |
| CSV | `.csv` | pandas | Row-by-row chunking |
| Excel | `.xlsx` | openpyxl | Sheet-by-sheet chunking |
| HTML | `.html`, `.htm` | BeautifulSoup | Text extraction + chunking |
| Image | `.png`, `.jpg` | Vision API (optional) | OCR + description |

---

## 38. Performance SLOs & Bottlenecks

### 38.1 Iteration Caps

| Setting | Value | Location | Notes |
|---------|-------|----------|-------|
| `MAX_TOOL_ITERATIONS` | 40 | agents.py:255 | Standard mode cap |
| `TOOL_CALL_HARD_CAP` | 10 | agents.py:264 | Per-turn tool call limit |
| `AGENT_MAX_ITERATIONS` | 100 | config.py:305 | Absolute agent loop limit |
| `AGENT_FAST_MAX_TOOL_ITERATIONS` | 10 | config.py:839 | **Fast mode (default)** — reduces 200s → 42-52s |
| `DASHBOARD_BUILD_MAX_TOOL_ITERATIONS` | 40 | config.py:849 | Dashboard uplift |
| `DELEGATE_MAX_ITERATIONS` | 5 | config.py:299 | Sub-agent delegation limit |
| `SWARM_MAX_CONCURRENT` | 8 | config.py:1328 | Max parallel swarm agents |

Fast mode is **on by default** (`AGENT_FAST_MODE_ENABLED=True`). This cuts per-turn time from ~200s to 42-52s by reducing LLM calls from 8-15 to 3-5.

### 38.2 LLM Call Timeouts

| Context | Timeout | Location |
|---------|---------|----------|
| `call_llm` async HTTP | **120s** | llm_service.py:560 |
| `stream_chat_completion` async HTTP | **120s** | llm_service.py:935 |
| Embedding / health probe (sync) | **30s** | llm_service.py:701, 757 |
| Deep research per-call | **90s** | config.py:388 |
| Self-eval LLM gate | **15s** (standard) / **5s** (fast) | config.py:802, 877 |
| Dynamic turn-plan LLM | **20s** | config.py:870 |

### 38.3 Wall-Clock Caps

| Scenario | Cap | Location |
|----------|-----|----------|
| Chat-only requests | **60s** | agents.py:13119 |
| File deliverable (docx/pptx/pdf/xlsx) | **180s** | agents.py:13119 |
| Dashboard builds | **1800s** (30 min) | agents.py:13117 |

### 38.4 Data Agent Budget

| Mode | Budget | Location |
|------|--------|----------|
| Standard | 60s | delegation_tools.py:71 |
| Fast mode | 30s | config.py |

### 38.5 Other Timeouts

| Setting | Value | Location |
|---------|-------|----------|
| `WHISPER_TIMEOUT_S` | 60s | config.py:242 |
| `CODE_EXECUTION_TIMEOUT` | 30s | config.py:292 |
| `DELEGATE_TIMEOUT` | 120s | config.py:300 |
| `AUTOMATION_RUN_TIMEOUT_SECONDS` | 600s | config.py:1363 |
| `SANDBOX_DEFAULT_TIMEOUT_SECONDS` | 120s | config.py:1401 |
| `SANDBOX_SKILL_TIMEOUT_SECONDS` | 240s | config.py:1431 |

### 38.6 Performance Profile (Typical Request)

| Request Type | LLM Calls | Tool Calls | Total Time |
|-------------|-----------|------------|------------|
| Simple chat (no tools) | 1 | 0 | 2-5s |
| Chat + 1 tool | 2 | 1 | 5-15s |
| Data query (fast mode) | 3-5 | 1-2 | 42-52s |
| Research loop (3 iters) | 4-6 | 3-5 | 60-120s |
| PPT generation | 3-5 | 2-3 | 30-60s |
| Dashboard build | 8-15 | 5-10 | 180-600s |

### 38.7 Known Bottlenecks

| Bottleneck | Impact | Mitigation |
|-----------|--------|------------|
| `agents.py` is 12.4k lines | Slow to load, hard to navigate | Decomposition planned (P2 gap) |
| Synthesis LLM call | 60-120s per call | Fast mode reduces frequency |
| Data agent sub-loop | 4 × 30s iterations | Auto-resolve `preferred_kb` when 1 KB bound |
| No parallel tool execution | Tools run serially | `asyncio.gather` exists for siblings but not for independent tools |
| Single-provider LLM | No failover if provider down | `LLM_FALLBACK_PROVIDERS` + health routing |
| No frontend watchdog | Spinner stuck forever | Needs read timeout on `streamAgentResponse` |
| `TOOL_ARG_REFORMULATION_ENABLED` | Adds 1 extra LLM call on failure | Only fires on permanent failures |

---

### C.19 Chat.jsx State Transitions — Exact Event Mapping

```javascript
// Simplified state transition table
// State: IDLE → STREAMING → [TOOL_PROGRESS | REFINING | PAUSED] → IDLE

function handleAgentSend(message) {
  // IDLE → STREAMING
  setMessages(prev => [...prev, userMsg, assistantMsg]);  // assistantMsg = {content: "", loading: true}
  setLoading(true);
  setStreamingId(assistantMsg.id);
  streamAgentResponse(message, {
    onDelta: (text) => {
      // STREAMING → streaming
      setMessages(prev => prev.map(m => 
        m.id === streamingId ? {...m, content: m.content + text} : m
      ));
    },
    onToolProgress: (tool) => {
      // STREAMING → TOOL_PROGRESS
      setMessages(prev => prev.map(m =>
        m.id === streamingId ? {...m, tool_calls: [...m.tool_calls, tool]} : m
      ));
    },
    onContentPreserve: () => {
      // STREAMING → REFINING
      setMessages(prev => prev.map(m =>
        m.id === streamingId ? {...m, refining: true} : m
      ));
    },
    onContentReplace: (text) => {
      // REFINING → STREAMING
      setMessages(prev => prev.map(m =>
        m.id === streamingId ? {...m, content: text, refining: false} : m
      ));
    },
    onPaused: (reason) => {
      // STREAMING → PAUSED
      setMessages(prev => prev.map(m =>
        m.id === streamingId ? {...m, paused: true} : m
      ));
    },
    onDone: () => {
      // STREAMING → IDLE
      setMessages(prev => prev.map(m =>
        m.id === streamingId ? {...m, loading: false} : m
      ));
      setStreamingId(null);
      setLoading(false);
    },
    onError: (msg) => {
      // STREAMING → IDLE (error)
      setStreamingId(null);
      setLoading(false);
      // Show error banner
    },
  });
}
```

---

### C.20 GATE Policy Evaluation — Decision Tree

```
evaluate_plan(plan, context)
│
├── Assess risk_tier
│   ├── All nodes are nl2sql (read-only)        → low
│   ├── Has sandbox nodes                        → medium
│   ├── Has configure_system nodes               → medium
│   ├── Has requires_confirmation nodes          → high
│   └── Otherwise                                → low
│
├── Apply policy rules
│   ├── risk_tier == "low" AND has_sandbox       → require_confirm (sandbox elevates)
│   ├── risk_tier == "low" AND NOT has_sandbox   → allow
│   ├── risk_tier == "medium"                    → require_confirm
│   ├── risk_tier == "high" AND has_approval     → require_confirm
│   └── risk_tier == "high" AND NOT has_approval → deny
│
└── Persist PolicyDecision (audit trail)
    ├── decision_level = "gate"
    ├── decision = allow/deny/require_confirm
    ├── risk_tier = low/medium/high
    └── reasons = [...]

evaluate_node(node, plan_decision, context)
│
├── If plan_decision == "deny"                   → deny (all nodes blocked)
│
├── node_type == "nl2sql"                        → allow (read-only always safe)
│
├── node_type == "sandbox"
│   ├── plan_approved == true                    → allow
│   └── plan_approved == false                   → deny
│
├── node_type == "configure_system"              → require_confirm
│
└── Otherwise                                    → follow plan_decision
```

---

## 39. Architecture Decision Records

Architecture Decision Records (ADRs) document the *why* behind key technical choices. Each ADR follows the format: **Context → Decision → Rationale → Consequences**.

### ADR-001: FSM-Governed Agent Loop over Raw Tool-Calling Loop

**Status**: Accepted (feature-flagged, `SYNEXIA_FSM_ENABLED=True`)

**Context**: The original agent loop was a raw LLM tool-calling cycle: call LLM → if tool_calls → execute → append result → call LLM again. This had no structure for planning, verification, or access control. The LLM could loop indefinitely, skip verification, or execute dangerous operations without oversight.

**Decision**: Introduce a Finite State Machine (Synexia FSM) with 9 states: `INIT → GOAL → CONTEXT → PLAN → GATE → ACT → OBSERVE → VERIFY → FINALIZE`. The FSM wraps the existing `execute_tool` function — tool behavior is unchanged, but the FSM adds plan/gate/observe/verify *around* the execution.

**Rationale**:
- **Structured decision-making** — each state has typed input/output contracts (ReportCard)
- **GATE policy evaluation** — risk assessment before tool execution (sandbox, configure_system)
- **VERIFY state** — self-evaluation of answer quality before returning to user
- **Observability** — each state transition is logged and streamable via SSE
- **Gradual rollout** — feature flag allows running FSM alongside legacy loop

**Consequences**:
- (+) Safety: dangerous tools require explicit GATE approval
- (+) Quality: VERIFY catches empty/degenerate answers
- (+) Auditability: state transitions are persisted
- (-) Latency: extra LLM calls for PLAN and VERIFY add ~15-30s per turn
- (-) Complexity: FSM adds ~2000 lines of orchestration code
- (-) VERIFY currently no-op in some paths (P0 gap: needs replan loop)

---

### ADR-002: Docker-Outside-of-Docker (DooD) for Sandbox Isolation

**Status**: Accepted

**Context**: The platform needs to execute untrusted code (skill scripts, PPT/DOCX generation, Python snippets). Three options were considered: (1) Docker-in-Docker (DinD), (2) Docker-outside-of-Docker (DooD), (3) process-level isolation (nsjail/firejail).

**Decision**: Use DooD — the sandbox worker container mounts the host Docker socket (`/var/run/docker.sock`) and spawns sibling containers for each job.

**Rationale**:
- **No privileged DinD container** — DinD requires `--privileged` or complex sysbox setup, which is a security risk in production
- **Host kernel sharing** — sibling containers share the host kernel (no nested virtualization overhead)
- **Path resolution** — DooD requires translating container-local paths to host paths via `docker inspect $HOSTNAME`, which is well-understood
- **Volume mount compatibility** — bind mounts resolve on the host filesystem, which is where MinIO uploads and sandbox I/O directories live
- **LLM proxy** — skill-driven containers need LLM access despite `--network none`; a Unix domain socket proxy mounted from the worker provides this without TCP/IP

**Consequences**:
- (+) Security: sandbox containers run `--network none --read-only --cap-drop ALL`
- (+) Performance: no nested virtualization overhead
- (+) Simplicity: standard Docker CLI, no custom runtime
- (-) Docker socket access = root-equivalent on host; sandbox worker must be tightly controlled
- (-) Path resolution complexity (DooD translation layer)
- (-) Cannot run sandbox worker in Kubernetes without docker-socket adapter

---

### ADR-003: DeepSeek as Primary LLM Provider

**Status**: Accepted (configurable via `LLM_MODEL`, `LLM_FALLBACK_PROVIDERS`)

**Context**: The platform needs a cost-effective LLM with strong tool-calling support for the agent loop. Options: (1) OpenAI GPT-4o, (2) Anthropic Claude, (3) DeepSeek Chat, (4) Local Ollama.

**Decision**: Use DeepSeek Chat (`deepseek-chat` via `https://api.deepseek.com/v1`) as the default primary model, with optional fallback to other providers.

**Rationale**:
- **Cost** — DeepSeek is ~15× cheaper per token than GPT-4o (~$0.14/1M input vs ~$2.50/1M input)
- **Tool calling** — DeepSeek supports structured function calling (OpenAI-compatible API)
- **CJK support** — Strong Chinese language performance for the Acme BI domain
- **Context window** — 64K tokens (sufficient for most agent loops with compaction)
- **OpenAI-compatible API** — Same `/v1/chat/completions` endpoint, zero migration cost for fallback

**Consequences**:
- (+) 10-15× cost reduction vs GPT-4o
- (+) CJK-native performance
- (-) Reasoning quality slightly below GPT-4o for complex multi-step tasks
- (-) API rate limits more aggressive than OpenAI
- (-) No native structured outputs (JSON mode only)
- Mitigation: `LLM_FALLBACK_PROVIDERS` + health routing for provider diversity

---

### ADR-004: Three-Layer Context Compaction over Sliding-Window Truncation

**Status**: Accepted

**Context**: Conversations can grow to 100+ messages with tool results. Simple approaches (sliding window, last-N-messages) lose important context. The LLM has a 64K token context window.

**Decision**: Implement a three-layer compaction engine:
1. **Layer 1 — Summary compression**: Old messages → LLM-generated summary
2. **Layer 2 — Tool result pruning**: Large tool outputs → truncated with key findings preserved
3. **Layer 3 — Relevance scoring**: Keep messages most relevant to current task (embedding-based)

**Rationale**:
- **Sliding window loses context** — dropping messages by recency loses important early context (user requirements, domain constraints)
- **Summary preserves intent** — compressed summaries retain the "what" even when details are dropped
- **Tool results are bloated** — SQL query results can be 10K+ tokens but only a few rows matter
- **Relevance > recency** — the most relevant message to the current task may be 20 turns back

**Consequences**:
- (+) 40-60% token reduction on long conversations
- (+) Important context preserved vs naive truncation
- (-) Extra LLM call for summary generation (adds ~5s latency on compaction trigger)
- (-) Summary quality depends on LLM — may lose nuance
- (-) Compaction engine is complex (~1500 lines) and only used in the raw loop, not the FSM path (P0 gap: unify context management)

---

### ADR-005: Five-Layer Constitutional Prompt Architecture

**Status**: Accepted

**Context**: User-created agents need configurable behavior without code changes. A single "system prompt" field is too coarse — agents need identity, boundaries, reasoning strategy, tool preferences, and output format.

**Decision**: Split the system prompt into 5 constitutional layers on the `AgentApp` model: Identity → Boundary → Reasoning → Tools → Output. Each layer is independently editable by the user.

**Rationale**:
- **Separation of concerns** — identity changes don't affect reasoning strategy
- **User control** — non-technical users can edit specific layers without understanding the whole prompt
- **Composability** — layers are concatenated; empty layers are skipped
- **Auditability** — each layer has clear purpose, making prompt engineering debuggable
- **Market differentiation** — no other agent platform offers this level of prompt granularity

**Consequences**:
- (+) 5× more controllable than single-field system prompts
- (+) Enables "prompt engineering by layer" — specialists can tune specific aspects
- (-) Users may not understand which layer to edit (needs good UI labels)
- (-) No layer-level versioning (only `AgentApp.updated_at`)

---

### ADR-006: SSE over WebSocket for Agent Streaming

**Status**: Accepted

**Context**: The agent loop produces incremental output (text deltas, tool progress, phase changes). Options: (1) Server-Sent Events (SSE), (2) WebSocket, (3) Polling.

**Decision**: Use SSE (`text/event-stream`) for the agent streaming protocol.

**Rationale**:
- **HTTP-native** — SSE works over standard HTTP, no protocol upgrade needed
- **Firewall-friendly** — corporate proxies block WebSocket upgrades; SSE passes through
- **Auto-reconnection** — browsers auto-reconnect SSE streams on disconnect
- **Unidirectional** — agent streaming is server→client only; WebSocket's bidirectional capability is unnecessary
- **Frontend simplicity** — `EventSource` API is simpler than WebSocket for streaming text

**Consequences**:
- (+) Works behind corporate proxies and load balancers
- (+) Simple frontend implementation
- (+) No connection state management on server
- (-) Unidirectional only — user actions (continue, steer) require separate POST requests
- (-) No binary data — all events are text (sufficient for agent output)
- (-) No built-in timeout — frontend must implement watchdog (currently missing, known bug)

---

### ADR-007: FastAPI Single-Process over Distributed Task Queue for Main Loop

**Status**: Accepted (with harness as alternative for programmatic runs)

**Context**: The main agent chat loop runs 3-5 LLM calls and 1-3 tool calls per turn. Options: (1) single-process FastAPI async, (2) Celery distributed task queue, (3) Ray actors.

**Decision**: Use single-process FastAPI `asyncio` for the main chat loop. Use the Agent Harness (`AgentRunOrchestrator`) for programmatic runs that need checkpoint/resume.

**Rationale**:
- **Low latency** — no queue overhead, LLM calls stream directly to client
- **Simplicity** — single process, no broker dependency for the critical path
- **State management** — conversation state stays in-process (no serialization)
- **SSE streaming** — direct async generator to client, no intermediate queue
- **Celery for background only** — automation scheduling, drift checks use Celery

**Consequences**:
- (+) Sub-second first-token latency
- (+) Simple deployment (no Redis dependency for chat)
- (-) No horizontal scaling of individual conversations
- (-) Single-process bottleneck — all concurrent chats share one event loop
- (-) No automatic resume on process crash (only harness has checkpoint/resume)
- Mitigation: `AGENT_FAST_MODE_ENABLED` caps iterations at 10, preventing event loop starvation

---

### ADR-008: Feature-Flag-First Development

**Status**: Accepted

**Context**: The platform has 60+ tools, 10+ subsystems, and rapidly evolving agent architecture. Deploying untested features to production is risky.

**Decision**: Every new capability is gated behind a feature flag in `config.py`, defaulting to `False`. Flags use `getattr(settings, "FLAG", False)` — never `os.environ.get()`.

**Rationale**:
- **Gradual rollout** — enable features per-tenant or per-environment
- **Instant rollback** — set flag to False to disable without redeployment
- **A/B testing** — different flags for different user cohorts
- **Development safety** — incomplete features can be merged without breaking production

**Consequences**:
- (+) Zero-downtime feature toggles
- (+) Safe main-branch development
- (-) Flag proliferation — 50+ flags currently, needs periodic cleanup
- (-) Dead code paths — disabled features still exist in codebase
- (-) Testing complexity — must test both flag states

---

## 40. Log Format & Tracing Reference

### 40.1 Logging Configuration

**File**: `backend/app/main.py` — logging setup at application startup

The backend uses Python's `logging` module with `RotatingFileHandler`:

```python
# Logger name: "zhanlu"
# Handler: RotatingFileHandler
#   - File: backend.log
#   - Max size: 10 MB
#   - Backup count: 5
#   - Encoding: UTF-8
#   - Format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
```

**Known issue**: The `RotatingFileHandler` is attached to the `"zhanlu"` logger only. Code that logs under `app.services.*` or `app.routers.*` may not be captured. Fix: use `logging.getLogger("zhanlu")` or ensure propagation to the root logger.

### 40.2 Log Format

Each log line follows this structure:

```
2026-09-03 14:23:45,678 - zhanlu - INFO - Agent loop iteration 3/10: calling LLM (model=deepseek-chat)
│                        │         │      │
│                        │         │      └── Message
│                        │         └── Level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
│                        └── Logger name ("zhanlu")
└── Timestamp (local time, ISO-ish format)
```

### 40.3 Key Log Patterns to Grep

| What You're Looking For | Grep Pattern | Source |
|------------------------|--------------|--------|
| Agent loop iterations | `Agent loop iteration` | agents.py |
| LLM call start/duration | `llm_call\|LLM call\|call_llm` | llm_service.py |
| Tool execution | `tool_call\|execute_tool\|tool_result` | agent_tools.py |
| Tool retry | `retry_with_backoff\|reformulate` | tool_retry.py |
| Circuit breaker | `circuit_breaker\|provider_health` | provider_health.py |
| FSM state transitions | `FSM transition\|synexia\|fsm_state` | fsm.py |
| GATE decisions | `gate\|policy_decision\|risk_tier` | policy_evaluator.py |
| Memory operations | `memory_snapshot\|learning_graph\|AgentMemory` | memory_tool.py |
| Sandbox jobs | `sandbox\|container_name\|SandboxJob` | sandbox_service.py |
| Auth events | `auth\|login\|token\|rate_limit` | auth.py |
| DB errors | `psycopg2\|InterfaceError\|UndefinedColumn` | SQLAlchemy |

### 40.4 Request Tracing

**Problem**: There is no built-in request correlation ID in the current codebase. Requests cannot be traced across components (frontend → backend → LLM → tool → DB) with a single ID.

**Workaround**: Correlate by timestamp + user_id + conversation_id:

```bash
# Find all logs for a specific conversation
docker logs zhanlu-backend 2>&1 | grep "conversation_id=abc123" | tail -100

# Find all LLM calls in the last 5 minutes
docker logs zhanlu-backend --since 5m 2>&1 | grep "call_llm\|LLM call"

# Find tool execution errors
docker logs zhanlu-backend 2>&1 | grep -i "error\|failed\|exception" | grep "tool" | tail -50
```

### 40.5 Structured Logging (Partial)

Some modules use structured logging via the tracing subsystem:

**File**: `backend/app/services/tracing/`

```python
# TraceContext — thread-local context for request metadata
class TraceContext:
    @staticmethod
    def current_role() -> str | None: ...
    @staticmethod
    def current_user_id() -> str | None: ...
    @staticmethod
    def current_conversation_id() -> str | None: ...

# Span helpers
def note_llm_usage(model, prompt_tokens, completion_tokens, duration_ms): ...
def llm_completion_span(model, messages, tools): ...
def traced_llm_call(model, task_type): ...
```

### 40.6 Frontend Logging

**File**: `frontend/src/api/agentEnhanced.js`

Frontend logs SSE events to the browser console:

```javascript
// On each SSE event:
console.log(`[AgentStream] event: ${event.type}`, data);

// On error:
console.error(`[AgentStream] error:`, error);

// On stream end:
console.log(`[AgentStream] stream ended`);
```

**Known gap**: No structured frontend logging; no error reporting service integration.

### 40.7 Improving Observability (Roadmap)

| Improvement | Priority | Effort |
|-------------|----------|--------|
| Add request correlation ID (UUID per stream) | P0 | Low |
| Fix logger scope (all code under "zhanlu") | P0 | Low |
| Structured JSON logging (for ELK/Datadog) | P1 | Medium |
| Frontend error reporting (Sentry) | P1 | Low |
| LLM call token tracking per conversation | P1 | Medium |
| Distributed tracing (OpenTelemetry) | P2 | High |

---

## 41. Contributing Guide

### 41.1 Code Style — Python

**Import ordering** (enforced by Ruff, rule E4):

```python
# 1. Future imports (optional)
from __future__ import annotations

# 2. Standard library
import json
import logging
from datetime import datetime

# 3. Third-party
import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

# 4. Local (app.*)
from app.config import settings
from app.deps import get_db
from app.models.user import User
```

**Naming conventions** (PEP 8):

| Element | Convention | Example |
|---------|-----------|---------|
| Functions | `snake_case` | `build_llm_payload`, `check_rate_limit` |
| Classes | `PascalCase` | `AuthService`, `LlmModel`, `TimestampedBase` |
| Methods | `snake_case` | `hash_password`, `verify_token` |
| Variables | `snake_case` | `access_token`, `user_id` |
| Constants | `UPPER_SNAKE_CASE` | `_REDIS_KEY_PREFIX`, `_SYSTEM_META_AGENTS` |
| Private helpers | `_leading_underscore` | `_admin_only`, `_check_enabled` |
| Pydantic schemas | `PascalCase` class, `snake_case` fields | `LlmModelCreate` |
| DB tables | `snake_case` | `__tablename__ = "agent_apps"` |

**Type hints**: Required on all function signatures. Use `Mapped[]` for SQLAlchemy columns, `Optional` for nullable, `str | None` (Python 3.10+) preferred over `Optional[str]`.

```python
# Good:
async def get_user(user_id: str, db: Session) -> User | None:

# Bad:
async def get_user(user_id, db):
```

**Docstrings**: Google-style:

```python
def execute_tool(tool_name: str, args: dict, db: Session, user_id: str | None = None) -> dict:
    """Execute a tool by name.

    Args:
        tool_name: Name of the tool to execute.
        args: Tool arguments as a dictionary.
        db: Database session.
        user_id: Optional user ID for authorization.

    Returns:
        dict with "success" key and "result" or "error" key.

    Raises:
        ToolNotFoundError: If tool_name is not in the registry.
    """
```

### 41.2 Code Style — Frontend (React/JavaScript)

| Element | Convention | Example |
|---------|-----------|---------|
| Components | `PascalCase.jsx` | `Chat.jsx`, `DataSourcesSection.jsx` |
| Hooks | `camelCase` | `useAuth`, `useDashboardStream` |
| Event handlers | `handle` prefix | `handleAgentSend`, `handleDelete` |
| State variables | `camelCase` | `streamingId`, `activeSkill` |
| CSS classes | Tailwind utility classes | `className="flex items-center gap-2"` |
| Icons | lucide-react | `import { RefreshCw } from "lucide-react"` |

### 41.3 Linting

**Backend** — Ruff (`backend/ruff.toml` or `pyproject.toml`):

```bash
# Run linting
cd backend && ruff check .

# Run formatting
cd backend && ruff format .
```

Key rules enabled: E4 (import conventions), F (pyflakes), W (warnings), I (isort), N (naming), UP (upgrade).

**Frontend** — ESLint + Prettier:

```bash
cd frontend && npx eslint src/
cd frontend && npx prettier --check src/
```

### 41.4 Testing Conventions

**File naming**: `test_{feature}.py` in `tests/` directory

**Test structure**:

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Fixtures — SQLite in-memory for unit tests
@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

# Test naming: test_{what}_{condition}_{expected}
def test_reingest_failed_kb_returns_success(db):
    """Re-ingesting a failed KB should return success with chunk_count."""
    ...

def test_reingest_nonexistent_kb_returns_404(db):
    """Re-ingesting a nonexistent KB should return 404."""
    ...
```

**Running tests**:

```bash
# Local (with venv)
cd backend && venv/bin/python -m pytest tests/test_kb_reingest.py -p no:jaxtyping -p no:cacheprovider

# Container
docker exec zhanlu-backend /usr/local/bin/python -m pytest tests/test_kb_reingest.py -p no:jaxtyping -p no:cacheprovider

# No parallel pytest — limited RAM in containers
```

**Important flags**: `-p no:jaxtyping -p no:cacheprovider` — these plugins cause issues in the container environment and must be disabled.

### 41.5 Git & Branch Conventions

| Convention | Pattern |
|-----------|---------|
| Branch naming | `fix/short-description` or `feat/short-description` |
| Commit messages | Imperative mood: "Add KB reingest endpoint" not "Added..." |
| PR size | Small, focused — one feature or one fix per PR |
| Main branch protection | No force push to main/master |

### 41.6 Adding a New Router — Checklist

1. Create `backend/app/routers/my_router.py` with `router = APIRouter()`
2. Register in `backend/app/main.py`: `app.include_router(my_router.router, prefix="/api")`
3. **Verify import**: `docker exec zhanlu-backend python -c "from app.routers.my_router import router"`
4. **Restart backend**: `docker restart zhanlu-backend zhanlu-sandbox-worker`
5. Test the endpoint

**Critical gotcha**: Router `ImportError` is **silent** in FastAPI — if your router has a typo or missing import, it won't crash the server, it just won't be registered. Always verify with step 3.

### 41.7 Adding a New DB Model — Checklist

1. Create model in `backend/app/models/my_model.py`
2. Import in `backend/app/models/__init__.py` so Alembic detects it
3. Add tenant columns: `org_id = Column(String, nullable=False, index=True)`
4. Add `_apply_tenant()` filter to all queries
5. Generate migration: `docker exec zhanlu-backend alembic revision --autogenerate -m "add_my_table"`
6. Apply: `docker exec zhanlu-backend alembic upgrade head`
7. Restart: `docker restart zhanlu-backend`

### 41.8 Development Workflow

```bash
# 1. Start the stack
docker compose up -d --build

# 2. Backend development (bind-mount, but modules need restart)
#    After editing Python files:
docker restart zhanlu-backend zhanlu-sandbox-worker

# 3. Frontend development
cd frontend && npm run dev   # → :5157 (Vite dev server, HMR)
# OR for production build:
cd frontend && ./node_modules/.bin/vite build --emptyOutDir=false && docker restart zhanlu-frontend

# 4. Check health
curl localhost:5002/healthz

# 5. Check DB info
curl localhost:5002/api/_db-info

# 6. View logs
docker logs zhanlu-backend -f --tail 100
docker logs zhanlu-frontend -f --tail 100
```
