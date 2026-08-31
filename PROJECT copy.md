# Zhanlu™ — Enterprise AI Operating System (Project Deep-Dive)

> **Project:** Zhanlu (展鹭) Enterprise AI OS, built by SYNEXIA (展鹿/赛内克夏)
> **Agent brain:** Synexia™ — the sole AI orchestration core
> **Repo:** `/Users/minhazulislam/Desktop/zhanlu_2026_08_26`
> **Snapshot date:** 2026-08-29
> **Scale:** ~218K LOC Python backend + ~68K LOC React frontend + ~200 markdown docs, 78 Alembic migrations, 643 backend test files, 325+ git commits (since 2026-07-31 recovery).

---

## Table of Contents

1. [What Zhanlu Is](#1-what-zhanlu-is)
2. [Tech Stack](#2-tech-stack)
3. [Repository Layout](#3-repository-layout)
4. [The Seven-Layer Architecture](#4-the-seven-layer-architecture)
5. [Backend Deep Dive](#5-backend-deep-dive)
6. [Frontend Deep Dive](#6-frontend-deep-dive)
7. [Infrastructure & Deployment](#7-infrastructure--deployment)
8. [Data & Storage Architecture](#8-data--storage-architecture)
9. [Authentication & Security](#9-authentication--security)
10. [Core Workflows (End-to-End)](#10-core-workflows-end-to-end)
11. [Domain Subsystems: Forecasting, BI, ERP, Intelligence, CAD](#11-domain-subsystems-forecasting-bi-erp-intelligence-cad)
12. [Skills, Capabilities & Marketplace](#12-skills-capabilities--marketplace)
13. [Dashboards](#13-dashboards)
14. [Testing & QA](#14-testing--qa)
15. [Docs & Planning Archive](#15-docs--planning-archive)
16. [Feature Timeline (July–August 2026)](#16-feature-timeline-julyaugust-2026)
17. [Known Issues & Gotchas](#17-known-issues--gotchas)
18. [How to Run](#18-how-to-run)
19. [AI Engineering & LLM Systems Perspective](#19-ai-engineering--llm-systems-perspective)

---

## 1. What Zhanlu Is

Zhanlu is an **enterprise AI operating system** — an industry-agnostic platform where
users chat with a main AI agent (the **Synexia** cognitive core) that plans, delegates
to sub-agents, queries connected databases, runs sandboxed code, and produces governed
**artifacts** (PPT, DOCX, XLSX, PDF, HTML dashboards, mini-apps) that are previewed
inline in the chat UI.

It is a Chinese-market, chemical-industry-first product in practice (Ecisco BI,
petrochemical price forecasting, market decks), but the platform itself is generic:
any industry, any database, any model.

### Product rules (non-negotiable, from `docs/00_FINAL_READ_ME_FOR_CODING_AGENT.md`)

1. **Synexia is the only AI orchestration brain** — every model call flows through it.
2. Users chat with a **main agent**; main agents may delegate to sub-agents under Synexia control.
3. Users select high-level **capabilities** (Make PPT, Database Analysis, Make Dashboard…), not raw internal skills.
4. Users connect databases **once** in My Space → Databases & KB; each agent may only use explicitly bound datasources (**DATA-CORE-3**).
5. Sub-agents inherit **no** database access by default; access is delegated by the main agent (**DATA-CORE-4**).
6. All DB access goes through a **Datasource Gateway** and creates **DataSnapshots**; the sandbox never receives raw credentials (**DATA-CORE-6**).
7. Generated files are **Artifacts**, not chat attachments; inline preview uses permission-checked APIs.
8. **PostgreSQL is the source of truth; Redis is temporary** (queues/cache/locks/events only); sandbox filesystems are destroyed after execution.
9. Every agent is a **Harness Agent** — a versioned, governed execution profile, not an independent reasoning service.

---

## 2. Tech Stack

### Backend
| Area | Choice |
|---|---|
| API framework | FastAPI 0.115 + Uvicorn, single `backend/main.py` entry |
| ORM / migrations | SQLAlchemy 2.0 + Alembic (78 revisions) |
| Data stores | PostgreSQL 16 (source of truth), Redis 7 (queue/cache/locks), MinIO (optional S3 object storage) |
| Models | Pydantic v2 + pydantic-settings |
| Auth | PyJWT (access + rotating refresh tokens), passlib/bcrypt, OTP email registration |
| LLM layer | OpenAI-compatible clients (`app/services/llm_service.py`), provider failover, health routing, task-based model routing, response caching |
| Sandbox | Docker SDK; `sandbox-worker` (only service with `/var/run/docker.sock`) |
| Docs generation | python-docx, python-pptx, reportlab, openpyxl, mammoth (docx→html), LibreOffice headless (previews), matplotlib |
| NL2SQL | sqlglot validation + SQLAlchemy adapters (MySQL/Postgres/SQLite) |
| Forecasting | XGBoost, statsmodels (ARIMA/ETS), VAR/VECM, foundation models (Chronos-Bolt, Moirai), Optuna tuning, conformal intervals |
| Other | ChromaDB (embeddings/RAG), docker SDK, httpx, croniter, jsonschema, mcp |

### Frontend
| Area | Choice |
|---|---|
| Framework | React 18 + Vite 6, JS (no TS in src), react-router-dom v6 |
| SDK glue | `@base44/sdk` 0.8.x + `@base44/vite-plugin` (legacy app shell) |
| UI | Tailwind CSS 3, Radix UI primitives (shadcn-style `components/ui`), framer-motion, lucide-react |
| Charts | recharts, three.js |
| Markdown / docs | react-markdown + remark-gfm, docx-preview, jspdf, html2canvas |
| Data | @tanstack/react-query, react-hook-form, zod |
| Layout | react-resizable-panels, @hello-pangea/dnd |
| Testing | Vitest (unit/component, 100+ suites), Playwright (e2e, zh-CN locale) |
| Mobile | Responsive mobile tree with phone-frame chrome (width ≤1024 + touch) |

---

## 3. Repository Layout

```
zhanlu_2026_08_26/
├── backend/                  # FastAPI application (~218K LOC Python)
│   ├── main.py               # App entry: 40+ routers, startup seeds, auto-migrations
│   ├── app/
│   │   ├── config.py         # ~1400-line Pydantic settings (feature flags galore)
│   │   ├── database.py       # SQLAlchemy engine/session (Postgres or SQLite)
│   │   ├── core/mysql_db.py  # MySQL adapter helpers
│   │   ├── models/           # 70+ SQLAlchemy models
│   │   ├── schemas/          # Pydantic schemas
│   │   ├── routers/          # 38 routers (auth, agents, artifacts, dashboards, ...)
│   │   ├── services/         # 80+ service packages (FSM, tools, forecasting, ...)
│   │   ├── dashboards/       # Generated full-stack dashboard app code (slug dirs)
│   │   └── static/vendor/    # vendored frontend assets
│   ├── sandbox_worker/       # Redis-queue Docker sandbox executor
│   ├── alembic/              # 78 migrations
│   ├── skills/               # bundled skills (~100 folders)
│   ├── system_skills/        # core skills (agent-builder-principles, harness-creation-rules)
│   ├── tests/                # 643 test files + suites (e2e, eval, nl2sql, ...)
│   ├── docker/               # backend + 4 sandbox Dockerfiles
│   └── requirements.txt
├── frontend/                 # React/Vite app (~68K LOC)
│   ├── src/                  # pages/, components/, api/, lib/, hooks/, utils/
│   ├── e2e/                  # Playwright specs + .auth storage state
│   ├── tests/                # Vitest config/setup
│   ├── base44/               # base44 serverless functions (generatePptx, generateReportDocx)
│   └── playwright.config.cjs
├── docs/                     # ~200 architecture/plan/spec docs (see §15)
├── scripts/                  # 50+ ops/dev scripts (prestart, e2e, probes, backfill)
├── infra/                    # postgres init.sql, redis.conf, minio init.sh
├── deploy/                   # frontend.nginx.conf + compose override template
├── docker-compose.yml        # backend + sandbox-worker + postgres + redis + minio + sandbox images
├── docker-compose.override.yml
├── Makefile                  # up/down/build/logs/db-*/smoke targets
├── .env / .env.example / .env.production
├── ZHANLU_FINAL_SPEC.md / ZHANLU_FINAL_SPEC_UNIVERSAL.md   # context-safety specs
├── README.md                 # auth + deployment notes
├── agent_qa/                 # QA artifacts (PPTX decks, traces, screenshots)
├── outputs/                  # generated deliverables
├── data/                     # sandbox tmp, repro scripts, macro overrides
└── uploads/                  # user uploads
```

---

## 4. The Seven-Layer Architecture

The canonical architecture is documented in
`docs/07_existing_architecture/Zhanlu_Layer_{1..7}_*.md` and the master
`Zhanlu_Agent_Skill_MCP_MultiAgent_Swarm_Artifact_System_FINAL.md`.

| Layer | Name | Responsibility |
|---|---|---|
| **1** | Enterprise Interaction & Identity | Web/mobile/voice/API channels; identity & tenant resolution; app/workspace switching; private conversations; **RequestEnvelope** (server-sealed identity+context); inline artifact preview; confirmation cards |
| **2** | Synexia™ Cognitive Core | One governed **plan-act-observe FSM loop** with seven named engines (Goal, Context, Planning, Reasoning, Decision, Reflection, Learning). Consumes RequestEnvelope → **TaskSpec** → **ContextManifest** → **PlanDAG** → policy gates → routes steps → **ObservationRecords** → deterministic confidence |
| **3** | Enterprise Harness Agent, Skill & Data Runtime | Harness Agent profiles (versioned execution profiles), Agent Studio, Skill Studio, Datasource Studio, Tool/Skill Gateway, governed NL2SQL, skill discovery/factory |
| **4** | Enterprise Memory, Knowledge & Context Intelligence | Working/private/app/org memory, document memory, DataSnapshot store, artifact knowledge, decision memory, enterprise knowledge graph, semantic data model, hybrid retrieval + rerank + **Memory Gate** → ContextManifest |
| **5** | Enterprise Execution Layer | Workflow engine, automation engine, sandbox execution, artifact generation (PPT/DOCX/PDF/XLSX/MD/HTML/dashboard/mini-app), preview generation, approval gates, notifications, retry/recovery, execution events |
| **6** | Enterprise Platform Services | Identity & access, tenant management, security & privacy, observability, cost/FinOps, governance & risk, AI governance (model/prompt/eval), platform governance — the shared control plane |
| **7** | Infrastructure | Docker-first: Docker Compose stack, PostgreSQL, Redis, MinIO, Nginx, backend/sandbox workers; Kubernetes-ready later |

**Core principle:** *thin model brain, strong harness, typed contracts, governed tool
dispatch, human-in-the-loop gates, traceable executions, artifact-aware outputs.*

---

## 5. Backend Deep Dive

### 5.1 Entry point (`backend/main.py`)

- Imports all models, registers `app.services.tool_handlers` (144 tools), mounts 38 routers under `/api`.
- Startup (`lifespan`): Postgres auto-migrations (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), ensures system agents + superadmin, normalizes agent prompts, syncs marketplace skills, starts scheduled tasks + automation dispatcher + catalog watchdog + dashboard app manager.
- `GET /healthz`, `GET /`, `GET /api`, `GET /api/_db-info` diagnostics.

### 5.2 Config (`app/config.py`, ~1,400 lines)

Hundreds of environment-driven feature flags. Highlights:

- **Required `DATABASE_URL`** — no silent default; refuses to start without it.
- LLM: `OPENAI_BASE_URL/API_KEY/LLM_MODEL`, `LLM_FALLBACK_PROVIDERS` (JSON), `LLM_FIXED_TEMPERATURE_MODELS` (`kimi-k2,o1,o3,qwen3,deepseek-v4`), `LLM_MAX_TOKENS_HARD_CAP` (4096), `LLM_SYNTH_MAX_TOKENS` (6144), embeddings, `SEARCH_PROVIDER` (tavily/serper/bocha/duckduckgo/bing), image gen (openai/fal).
- **Context economics**: per-model tool-output caps (`TOOL_OUTPUT_CAP_BY_MODEL`), `CONTEXT_ESCALATION_LADDER` (compact → truncate tool outputs → drop old tool messages → fallback model), `DSR_COMPACT_MODE_MAX_CONTEXT`.
- **Synexia FSM**: `SYNEXIA_FSM_ENABLED` (default on in prod env), verifier LLM, ACT re-plan depth (`SYNEXIA_ACT_REPLAN_MAX=2`), VERIFY re-plan (`SYNEXIA_VERIFY_REPLAN_MAX=1`), quality gate thresholds (0.4 chat / 0.6 automation), quality eval loop.
- **PPT pipeline**: deck planner, smart router, design-by-default renderer, blocking audit gate, LLM polish, deck profiles, edit tools routing, hero art, transitions, dynamic document planning + LLM narrative.
- **Deliverable guarantees**: strict artifact-match, turn guards, goal contract, clarify-suspends-turn, phase-lock + query-purpose tagging, plausibility checks, NL2SQL fanout guard, dashboard fuzzy match.
- **Forecasting MLOps**: ~40 `FORECAST_*` flags (foundation models, regime pool, HITL, drift auto-adjust, bias correction, threshold autotune, decision logging…).
- **Eval gate**: golden-eval regression gate (409 block on admin model change without parity), `EVAL_GATE_ENABLED`, floors/parity tolerance.

### 5.3 The Synexia FSM (`app/services/synexia/`)

States: `INIT → GOAL → CONTEXT → PLAN → GATE → ACT → OBSERVE → VERIFY → FINALIZE → (QUALITY_EVAL) → DONE`, plus `FAIL`.

1. **INIT** — creates an `Execution` row.
2. **GOAL** — `task_spec_parser` → `TaskSpec` (task_kind, artifact intents, deliverable format, requires_data, follow-up detection with `reuse_prior_data` → fast synthesis-only plan).
3. **CONTEXT** — `context_assembler` builds memory/KB/attachment manifest.
4. **PLAN** — `plan_dag` builds a PlanDAG (nodes of type skill/tool/nl2sql/sandbox/agent with dependencies); LLM planner opt-in; default plans per task_kind are the proven path.
5. **GATE** — `policy_evaluator` → allow/deny + risk tier.
6. **ACT + OBSERVE** — `capability_router` executes nodes (tool/nl2sql/synthesize/sandbox/skill), producing ObservationRecords; in-ACT adaptive re-plans on recoverable failures.
7. **VERIFY** — deterministic checks (artifact exists, observation success, data integrity, degenerate result, wrong grain, coverage, source citation, hallucination) + optional LLM rubric; bounded re-plan loop.
8. **FINALIZE** — `confidence_scorer` → deterministic confidence; response generation; QUALITY_EVAL corrective loop; artifact suppression gate (only ship files when user asked for a file); export finalize into artifacts.

### 5.4 Agent loop & reliability (`app/services/agent_loop/`, `reliability`)

- `fsm_pruner.py` — strips orphaned tool_calls (400 errors).
- Guardrails: exact-failure block after 5, same-tool failure halt after 8, no-progress block after 5.
- Iteration budget (per-conversation, thread-safe, refund on execute_code; `AgentApp.max_call_count` override).
- Tool-result persistence: 3-layer — per-tool 8K chars cap → disk >20K → per-turn aggregate spill >80K; read_file pinned.
- `api_error_classifier` — 15-reason FailoverReason enum; `tool_result_classification` single source of truth for idempotent/mutating tools; `pre_api_prune` 3-pass dedup/summarize/truncate.
- Post-turn: message sanitization, verification stop (max 2 nudges), background review every ~5 turns.
- P3 prompt caching (cache_control breakpoints); agent metrics via `GET /api/_agent-metrics`; scheduled tasks (memory consolidation 30min, skill curation 6h, alert checks 5min) + 7 threshold alerts.

### 5.5 Tool registry & handlers (144 tools)

`app/services/tool_registry.py` — decorator registration, role-based filtering via `TraceContext`, OpenAI function-schema normalization, toolset bucketing.

Key families:

- **Data**: `ask_data_agent`, `fetch_data_batch`, `execute_query/execute_sql/sql_query`, `comprehensive_data`, `collect_enterprise_data`, forecast tools.
- **Artifacts**: `create_artifact` (2K+ lines, guards + deck data flow), `edit_artifact`, deck edit tools, export service.
- **Sandbox/code**: `run_sandbox_skill`, `sandbox_code`, `execute_code`, computer use.
- **Web**: `web_search`, `web_extract`, `x_search`, `agent_browser` (navigate/click/type/snapshot), vision, transcription, TTS, image/video generation, `mixture_of_agents`.
- **Skills**: `skills` meta-tool, load/list skills, skill_manager, skills_hub, skills_sync, skills_guard.
- **Dashboards**: `create_dashboard`, `update_dashboard`, `create_fullstack_dashboard`, `update_fullstack_dashboard`, `revert_fullstack_dashboard`.
- **Delegation/swarm**: `delegate_task`, `swarm_*` (create_team, spawn_agent, send_message, get_messages, list_teams, scratch, orchestrate).
- **Memory/agents**: `memory`, `project_memory`, `session_search`, `todo`, `kanban`, `cronjob`, `clarify`, `interrupt`, approval, `send_message`, channels (discord/homeassistant/feishu_doc/feishu_drive/microsoft_graph).
- **MCP**: `mcp`, `mcp_oauth`, `mcp_oauth_manager`.
- **Fusion 360 CAD (~60 tools)**: execute_python, ping, probe, sketch primitives (line/circle/rectangle/polygon/arc/spline/arc_3point), extrude/revolve/mirror/fillet/chamfer/thread/clear, solids (box/cylinder/sphere/torus/loft/sweep/shell/combine/hole/pattern), assembly (components/joints), parametric (params_list, user_parameter, declare_spec), verify_build (contract checks with tolerance), io (save/import_dxf/project/export_geometry/make_drawing), measure/physical_properties.

### 5.6 Artifact system

- **Models**: `Artifact` (typed: pptx/docx/pdf/md/html/html_report/chart/dashboard/mini_app/image/xlsx; status draft→building→preview_ready→editing→validated→approved→published; canonical_format; source agent|dashboard_app), `ArtifactVersion` (immutable, source_json for partial regen), `ArtifactBlob` (original/preview/thumbnail/format_export; SHA-256; BYTEA or MinIO), `MessageArtifact` (message↔artifact links), `ArtifactSourcePart` (slide/chart/table granular parts).
- **Storage**: `BlobStorage` ABC — `postgres_bytea` (default, inline:// URI) or `minio` (s3://). Production mandates MinIO.
- **Preview**: LibreOffice headless PPTX/DOCX/XLSX→PDF, MD→HTML, HTML stays; permission-checked preview APIs; `ExportService` cached format exports (`get_or_render`, eager default render at finalize).
- **Pipeline**: `render_dispatcher`, `html_slide_generator`, `html_to_pptx`, `layout_engine`, `deck_planner/deck_data/deck_router/deck_profiles`, themes/palettes/brand_kit, audits/repairs (P0 audit/repair loop), thumbnails.

### 5.7 Sandbox (`sandbox_worker/main.py` + `app/services/sandbox/`)

- Only service with Docker socket access. Polls Redis `sandbox:queue` (BRPOP), fallback Postgres `sandbox_jobs`.
- Image map: pptx → `zhanlu-sandbox-pptx`; docx/xlsx/pdf → `zhanlu-sandbox-office`; md/html/chart/dashboard → `zhanlu-sandbox-python`; skill_runner → `zhanlu-sandbox-skill`.
- Flow: job → temp workspace → `prepare_input_package` (input JSON + runner script/modules base64) → container run (read-only input mount, resource limits, chmod 777 output) → collect outputs → events to Postgres + Redis pub/sub → artifact blobs → container destroyed.
- Skill-driven jobs mount an **LLM proxy socket** so sandboxed skill runners can call the LLM without keys; `_execute_in_process` fallback when Docker is unavailable.

### 5.8 Datasource & NL2SQL layer

- `services/datasources/`: `DatasourceAdapter` Protocol — MySQLAdapter, PostgresAdapter (schema-aware, EXPLAIN cost), SQLiteAdapter; `test_connection/list_tables/describe_table/refresh_schema/explain/query`; connection-fingerprint schema cache with KB-connect invalidation.
- `services/nl2sql/`: 599-line orchestrator — semantic_resolver (metric + mapping match) → validator (sqlglot + allow-list, dialect rules, row filter) → EXPLAIN cost → policy (allow-list/cost/row-limit) → query → audit events.
- `data_snapshot/`: immutable evidence — NL question → LLM SQL → read-only validation → execute → `DataSnapshot` (checksum, TTL 90d, SnapshotArtifactLink). **Artifacts cite DataSnapshot IDs, never live queries.**
- `data_source_runtime/`: resolves bound KB ids (agent KBs ∪ project KBs ∪ pinned data_source_id) and threads them into tool calls.

### 5.9 Auth & RBAC

- Email+password only; OTP-gated registration (first user bypass); access JWT 15 min with `jti` + rotating 30-day refresh token (SHA-256 stored in `refresh_tokens`); logout blacklists JTI + invalidates all refresh tokens.
- Rate limits per IP (login 5/min, register/OTP/reset 3 per 10 min).
- Feishu (Lark) QR login supported via config (disabled by default).
- Multi-tenant RBAC: `resource_shares`, `access_policies`, `authz/`, `permissions/`, `governance/` — resource-level control for agents/KBs/files/flows/reports; admin users/evals/invocations routers.

### 5.10 Other services (quick map)

| Service | Role |
|---|---|
| `enterprise_orchestrator/` | Dynamic Enterprise Business-Data Executive: parallel facet profiling (3–6 facets), per-facet isolation, synthesis, claim tracking |
| `knowledge_graph/` | Product value-chain graph (config-driven), catalog indexer, join-edge detector, schema linker, entity extractor, freshness, metric bootstrap; `ask_knowledge_graph` tool |
| `swarm/` | Team registry, mailboxes (priority queues), result-driven re-dispatch + escalation, aggregated summaries |
| `memory/` + `memory_advanced/` | Semantic dedup (cosine ≥0.85), consolidation, lifecycle archive/promote; vector store; project memory |
| `forecasting/` | See §11 |
| `dashboard_app/` | See §13 |
| `intelligence/` | **Source files removed (pyc shells)** — former news/event intelligence (event extractor, causal narrative, scenario probability) now consumed via models + scheduled tasks |
| `erp/` | **Source files removed (pyc shells)** — ERP behavior now in `domain_config.py`, `dashboard_profiler.py`, `enterprise_orchestrator`, `db_tools`/`enterprise_data_tools` |
| `weekly_digest/` | Scheduled digest reports |
| `channels/`, `notification_gateway/` | Feishu/Discord/etc. channels; email gateway for automation results (SMTP, signed download links, retries) |
| `document_ingestion/` | PDF/DOCX/TXT + audio (Whisper) + image (OCR) ingestion into KBs |
| `tracing/` | OpenTelemetry-style trace context (TraceContext), agent metrics |

---

## 6. Frontend Deep Dive

### 6.1 Route map (`src/App.jsx`)

Eager: `/`, `/chat`, `/login`, `/register`, `/forgot-password`, `/reset-password`.
Lazy code-split: `/automation`, `/automation/:id`, `/my-space`, `/my-space/project/:id`, `/my-space/agent/:id`, `/my-space/:type/:id`, `/market`, `/market/:id`, `/toolkit`, `/toolkit/:id`, `/skill-agent`, `/skills/executions`, `/agent-builder`, `/settings`, `/admin/users`, `/admin/observability`, `/dashboard/:id`, `/my-files`, `/from-personal`, `/from-company`, `/ui-test` (auth-bypass), mobile routes under `MobileFrame`.

### 6.2 Chat (`src/pages/Chat.jsx`, ~3,365 lines) — the heart

- **Session model**: ChatSessionContext (sessions, activeId, pendingProject), deep links `?session=` / `?conv=` / `?project=`; every chat auto-resolves `general_assistant` (or `automation_agent` for automation messages).
- **Streaming**: `streamAgentResponse` SSE generator; handles events `fsm_state`, `plan_summary`, `plan_step_added/completed` (live todo checklist), `reasoning_delta` (Live one-liner), `tool_progress`, `activity_step` (Manus-style feed), `live_event`, `phase`, `artifact_created`, `trace_step`, `delta`, `content_preserve/content_replace` (multi-iteration refine), `steer`, `error`, `done`.
- **Mid-turn steer**: if a v3 stream is in flight, new sends route to the steer endpoint (optimistic "→ steer" markers).
- **Stop button**: AbortController; appends "[Stopped generating.]"; separate automation cancel path.
- **Execution timeline**: LiveActivityStream — Kimi/Claude-style typed feed with tool_call pairs, subagent branches (nesting), inline 3×5 data preview, plan summary card, duration tickers, retry lineage, sticky running-row highlight; auto-collapses when terminal.
- **Artifacts**: MessageBubble renders ResultCard/DataTableCard/ReportCard/ArtifactPreviewCard/InlineArtifactPreview; right-side resizable ArtifactPanel + ArtifactPreviewPane (DOCX via docx-preview, PPTX via PptxArtifactPreview, PDF, HTML via DOMPurify, dashboards embedded), ArtifactCanvasEditor (user-editable HTML canvas).
- **Feedback**: thumbs up/down per message; role-relevance 1–5 every 3 messages.
- **Extras**: ScheduledPanel, ChatFilesModal, AutomationDraftCard, ClarifyOptions forms, category chips (production/maintenance/quality/safety/supply/energy), model switcher, draft persistence, message dedupe, dashboard-mode badge, project context breadcrumb.

### 6.3 Dashboard experience

- `DashboardView`: tries full-stack app record (`getDashboardApp`) → FullStackDashboardViewer (iframe hosting a FastAPI-served React app at `/api/dashboards/apps/{slug}/`, WebSocket live status); falls back to legacy `DashboardViewer`.
- Legacy viewer: `POST /api/dashboards/:id/query` with from/to/filters/drill, polling (10–300s clamp), cross-widget filter chips, drill-down, date presets; edit mode with WidgetEditorDrawer, native DnD reorder, PNG export (html2canvas), dirty guards.
- Widgets: KPI, Trend, Pie, Table, Area, StackedBar, Scatter, Radar, Gauge — all in WidgetShell; interactions resolved via `interactions.js`.
- Chat integration: dashboard-mode chats get a badge; `watchForDashboardEdits` turns completed dashboard tool_calls into first-class artifacts; docked panel refreshes after agent edits.

### 6.4 Builders

- **AgentBuilder** (`/agent-builder`): conversational agent creation; suggestion cards, prefill (`?prefill=`, `?edit=`), permission-mode toggle, streaming; created agents → preview or run.
- **AgentConfig** (`/my-space/agent/:id`): full editor — 5-layer constitution prompts (identity/boundary/reasoning/tools/output), skills + KB bindings, topology (standalone/sequence/loop/parallel), sub-agent tree CRUD, flow DAG editor, limits, access flags, capabilities, Fusion bridge section.
- **SkillAgent** (`/skill-agent`): conversational skill authoring — collect (URL scrape), create, learn (files), edit; live SKILL.md folder tree, draft editor, execution recording.
- **AutomationTasks**: month calendar + table, project filter, create/pause/resume/Run-now (runs stream into owning chat), execution status drawer + live status bus.

### 6.5 API layer

- `api/base44Client.js`: @base44/sdk client wrapped in a lazy Proxy that intercepts 401s → single-flight refresh → retry.
- `api/authFetch.js`: central fetch with Bearer token, auto 401 refresh-retry, `authFetchOrThrow` raises typed `SessionExpiredError`.
- `api/agentEnhanced.js`: raw SSE streaming wrapper (v3 conversations), create/steer/confirm-decision.
- `api/dashboards.js`, `api/skillStudio.js`, `api/agentFeedback.js`, `api/agentInvocations.js`, `api/projectCatalog.js`, `api/accessPolicyApi.js`, `api/marketplace.js`.
- `lib/AuthContext.jsx`: proactive refresh at ~80% of TTL, tab-focus refresh; `lib/ChatSessionContext`, `lib/PersistentStreamContext` (stream survives navigation), `lib/dashboardStreamWatcher`, `lib/runAutomationTask`, `lib/cancelAutomationExecution`, i18n (zh/en).

### 6.6 Mobile

Separate tree under `MobileFrame` (phone chrome) with bottom tabs; `MobileChatPage` (single-column, reuses MessageBubble/ChatInput), `MobileMySpacePage`, `MobileMyFilesPage`; desktop detail pages reused inside `MobileDetailShell`.

---

## 7. Infrastructure & Deployment

### `docker-compose.yml` services

| Service | Port | Notes |
|---|---|---|
| `backend` (zhanlu-backend) | 5002 | API + agent FSM + dashboard pipeline; 4GB memory cap; bind-mounts repo; persistent user-skills volume |
| `sandbox-worker` (zhanlu-sandbox-worker) | — | **Only** service with docker.sock; BRPOPs `sandbox:queue`; spawns disposable containers; 512MB cap |
| `postgres` (zhanlu-postgres) | 5400→5432 | postgres:16-alpine, init.sql, 512MB cap, healthcheck |
| `redis` (zhanlu-redis) | 6300→6379 | redis:7-alpine with config, healthcheck |
| `minio` + `minio-init` | 9400/9401 | S3-compatible object store, `zhanlu-artifacts` bucket |
| `sandbox-python/office/pptx/webapp` | — | build-only services that produce images |

Two isolated networks: `app_net` (backend↔sandbox-worker) and `data_net` (data layer).

### Production override (`docker-compose.override.yml`)

- Adds `restart: unless-stopped` to long-running services and a `frontend` (nginx:alpine) container serving `frontend/dist` and proxying `/api` to backend on `WEB_PORT` (default 80).

### Hard rules (from README)

- Every backend deploy must restart BOTH `zhanlu-backend` and `zhanlu-sandbox-worker` (bind-mount never reloads imported modules).
- Frontend-only changes need no restart (dist is bind-mounted `:ro`).

### Makefile

`make up|down|down-v|build|logs|reset|db-check|db-clear|db-setup|db-reset|db-info|smoke|smoke-ui`

---

## 8. Data & Storage Architecture

(`docs/storage-architecture.md`)

- **PostgreSQL 16** — source of truth: 45+ app tables (public schema), plus `audit` (immutable audit trail via DB triggers on INSERT/UPDATE/DELETE) and `governance` schemas. PgBouncer (:6432) in production.
- **Redis 7** — namespaces: `sb:` sandbox queues, `evt:` pub/sub fanout, `lck:` distributed locks, `sess:` session cache. Auth required in prod; dangerous commands disabled.
- **MinIO** — buckets with lifecycle: `zhanlu-artifacts` (90d → archive 365d → expire), `zhanlu-uploads` (30d), `zhanlu-exports` (7d), `zhanlu-backups` (30d). Keys: `{org}/{app}/{artifact}/v{ver}/{blob}/{checksum}.{ext}`.
- **Naming conventions**: snake_case plural tables, `ix_/uq_/fk_/pk_` prefixes; DB roles `zhanlu_app` / `zhanlu_migrate` / `zhanlu_readonly`; least-privilege model.
- **Retention**: soft-deleted 30d, DataSnapshots 7d, sandbox temp 24h, completed jobs 90d; quotas enforced pre-INSERT (`governance.check_quota()`): 5GB artifact blobs/org, 1GB uploads/org, 500MB sandbox output/job.
- **Integrity**: SHA-256 checksums on artifact blobs + DataSnapshots verified on read; quarterly integrity scans.
- **Backups**: PG daily 03:00 pg_dump custom (30d), Redis RDB every 6h, MinIO weekly mirror; restore requires confirmation.

### Key models (70+)

Organizations, AppWorkspace, User, Project, ChatSession, ChatMessage, AgentApp (30+ column harness profile), AgentConversation, KnowledgeBase, Datasource, DataSnapshot, DataExecution, Artifact/ArtifactVersion/ArtifactBlob/MessageArtifact, AgentRun/AgentRunStep, Execution, SandboxJob, SkillProfile/SkillRun/SkillCandidate, MarketplaceSkill/Rating, AutomationTask/Execution/File, Report, DecisionFlow, MarketAgent, McpServer, Forecasting (targets/runs/accuracy), IntelligenceEvent, CadBuildContract, HookRule, ResourceShare/ResourceAccessPolicy, RefreshToken/RevokedToken, OtpCode, PasswordResetToken, LlmModel, PromptAbTest, ExperienceEntry, LearningProposal, QSqlExample, Terminology, MetricDefinition, SemanticMapping, KnowledgeCatalog, ProjectMemory, ProjectAgent, AgentMemory/AgentTodo, WorkspaceSetting, SessionState, SwarmMailbox, etc.

---

## 9. Authentication & Security

- **Every page and every mutating/LLM/data endpoint requires login** — no anonymous access (auth hardening, plan 2026-07-27).
- Access JWT 15 min (carries `jti`); refresh 30 days, rotated on each use, stored hashed; logout blacklists JTI (`revoked_tokens`) and invalidates all refresh tokens.
- Password policy: ≥10 chars, letter + digit (configurable).
- In-memory per-IP rate limits; `0` disables.
- Sandbox security: only `sandbox-worker` gets Docker socket; sandbox containers get no credentials; LLM proxy socket instead of keys; resource limits enforced.
- Datasource security: agent-scoped bindings enforced per-call (`_require_kb_id`, `bound_kb_ids`); unselected DBs blocked, not just hidden.
- Preview security: permission-checked APIs only; frontend never receives raw object paths.
- Audit: DB triggers → `audit.audit_trail` (immutable, INSERT-only, indefinite).
- RBAC: resource shares + access policies + admin endpoints (`/api/admin/users|evals|invocations`), superadmin bootstrap via env.

---

## 10. Core Workflows (End-to-End)

### Chat → artifact

```
User message
  → POST /api/apps/{appId}/agents/conversations/v3/{conv}/messages/stream
  → (cache check: semantic response cache may replay instantly)
  → Synexia FSM: GOAL (TaskSpec) → CONTEXT (manifest) → PLAN (PlanDAG)
  → GATE (policy) → ACT/OBSERVE (capability router: tool / nl2sql / sandbox / skill nodes)
  → VERIFY (deterministic checks + optional LLM rubric)
  → FINALIZE (confidence score, response stream, QUALITY_EVAL, artifact finalize)
  → SSE events: fsm_state, plan_summary, activity_step, delta, tool_progress,
    artifact_created, live_event, done
  → Chat UI: LiveActivityStream timeline + inline Artifact Preview Card
```

### Database question

```
Agent → ask_data_agent / execute_query (bound KB only)
  → NL2SQL: semantic resolve → sqlglot validate → EXPLAIN cost → policy → query
  → DataSnapshot (immutable, checksum) + DataExecution rows
  → large results spilled to cache pointer (cache_id) instead of context
  → exploration tools: data_describe / data_top_n / data_aggregate / data_filter
  → build_file / create_artifact (csv/xlsx/docx/pptx) from snapshot
```

### Sandboxed skill run

```
run_sandbox_skill → SandboxService → Redis sandbox:queue
  → sandbox-worker BRPOP → temp workspace → disposable container
  → output collected → artifact blobs → container destroyed
```

### Dashboard generation

```
"Make me a dashboard" → dashboard intent detectors (fuzzy match enabled)
  → create_fullstack_dashboard → DashboardAppGenerator
  → Jinja2 templates → api.py/queries.py/realtime.py + React dist
  → mounted at /api/dashboards/apps/{slug}/ → iframe preview in chat
```

### Automation run

```
Scheduled task → automation_dispatcher (croniter) → AutomationExecution
  → hidden automation_runtime agent → streams results into owning chat
  → NotificationGateway emails recipients (attachments ≤8MB, else signed link)
```

---

## 11. Domain Subsystems: Forecasting, BI, ERP, Intelligence, CAD

### 11.1 Forecasting (`app/services/forecasting/`, 40+ modules)

- **Discovery → quality → preprocess → walk-forward backtest → features → model pool → ensemble → honesty gate → guard → scenarios → cache**.
- Models: Naive, Seasonal naive, ETS, ARIMA, STL, mean reversion, XGBoost (≥90d history, Optuna + purged TS CV), panel model (cross-product global XGBoost with product embeddings), VAR/VECM, foundation models (Chronos-Bolt / Moirai / TimesFM behind flags).
- Ensemble: backtest-error softmax weights, auto-tuned temperature, floor weights, max-error-ratio gating.
- **Honesty gate**: if blended error worse than naive → ship naive; ±15% change clamp; stale-data checks; feedstock/value-chain coherence; event overlay (48h market events with direction/magnitude/confidence, horizon-scaled); bias correction (damped 35%, capped ±2.5%).
- Outputs: base/bull/bear scenarios via split-conformal intervals, `p_rise` probability, directional classifier with binomial significance test (p<0.01), trust tiers (High/Medium/Directional reference/Insufficient), **decision engine** (BUY only if p_rise ≥70% AND ≥3% move AND statistical edge; SELL ≤30%; else WATCH/HOLD — "wrong do-nothing is cheaper than wrong buy-everything"), top-5 XGBoost drivers, 7-section bilingual analyst brief (LLM writes prose; decision is 100% deterministic rules), decision log + ROI loop, nightly threshold auto-tuning (STAGED, human-promoted).
- Accuracy tracking (2026-08-08 report, **honest negative**): 7d MAPE ~9.4% walk-forward; 30d 19.7% (poor); directional accuracy 17.9% (worse than random) at that time; ensemble beat naive on only 3/22 products — drove the regime-aware pool + feature-selection + realized-tracking overhaul.

### 11.2 BI / Ecisco

Ecisco (NOT NovaChem) is the primary BI tenant: EDIA data warehouse (`aipdp_data_warehouse_prod`, MySQL `10.10.10.49`), tier-1/tier-3 data sourcing, upstream/downstream value-chain dashboards, project knowledge cache, CEO morning digest, weekly digest, midstream decision board, SKU-level forecasting, market decks (C5/C9 hydrocarbons market analysis PPTs in repo root).

### 11.3 ERP & Intelligence (note)

`app/services/erp/` and `app/services/intelligence/` have had their `.py` sources **removed** — only `__pycache__` shells remain. Their functionality now lives in `domain_config.py`, `dashboard_profiler.py`, `enterprise_orchestrator`, `db_tools`/`enterprise_data_tools`, and `models/intelligence.py` + scheduled tasks.

### 11.4 CAD Agent (Fusion 360) — newest workstream (08-28)

- Drives Autodesk Fusion 360 via a socket bridge (`host.docker.internal:9876`, ~60 `fusion360_*` tools).
- Tier-1: parametric named dimensions, **design-intent contracts** (`cad_build_contracts` model, conversation cascade), **constraint-closed sketches** (coincident constraints + profile reporting), `verify_build` contract validation (expected_params with 5% tolerance), extrude refuses sketches with no closed profile.

---

## 12. Skills, Capabilities & Marketplace

- **Skills are governed capability packages** (folder-based: `SKILL.md` + references/scripts), not independent agents. Sources: bundled (`backend/skills/`, ~100), user, marketplace, generated, builtin.
- `skills_loader` (1,187 lines): manifest parser, SkillsRegistry, progressive disclosure (name+summary injected; body loaded on demand), unified search, skill planner hook.
- `skill_routing`: meta-tool, token-budgeted catalog (Layer A ≤15,000 chars), priority tiers (user > marketplace > generated > builtin > bundled).
- **Capabilities mapping** (docs/09): normal users pick friendly capabilities (Make PPT, Database Analysis, Make Dashboard, Make DOCX, Generate Charts, Scheduled Reports…); backend maps capability → skills + sandbox requirements + subagent roles; raw skills hidden in Advanced Settings (admin-only).
- **Skill Studio**: create/collect/candidates/dry-run/execution-recording; skill drafts per conversation.
- **Marketplace**: external sources, signed skill content (publisher signatures), install/rate/restore; `skill_sync` from marketplace to DB.
- `system_skills/`: agent-builder-principles, harness-creation-rules, using-superpowers.

---

## 13. Dashboards

Two generations coexist:

1. **Legacy dashboards** (`Dashboard` model + `services/dashboards`): widget JSON, `/api/dashboards/:id/query`, 9 widget types, edit mode, drill/filters.
2. **Full-stack dashboard apps** (`services/dashboard_app/`, models `DashboardApp`/`DashboardVersion`): `DashboardAppGenerator` is the **only writer** — Jinja2 templates (StrictUndefined) render `api.py`/`queries.py`/`realtime.py` from a spec (slug, metrics, refresh interval, theme, style, filters, insights, layout sections, pages, panels, header/footer), plus a pre-built React dist. config.json never contains raw SQL; SQL lives in generated code. Stored in `backend/app/dashboards/{slug}/`, imported by `DashboardAppManager`. Cascade delete + versioning + realtime WS + edit/undo tools.
- Spec styles: `standard | chinese_bi | ceo | editorial`; panel types: kpi, line, bar, pie, table, area, gauge, radar (+ AI-analysis panels); design tokens resolved from `design-system.json` sidecars with dark-mode + chart palette derivation.
- DB-agnostic rule (hard): zero hardcoded identifiers, standard SQL only, `quote_ident()` with KB dialect, guard tests assert no demo table/column names in source.

---

## 14. Testing & QA

- **Backend**: 643 test files under `backend/tests/` — routers, services, e2e, eval, nl2sql, q_sql_examples, retrieval, terminology, migrations, datasources. Conftest fixtures; `recognize_existing_test.py` for test discovery.
- **Frontend**: 100+ Vitest suites colocated (heavy Chat.jsx regression coverage: stopButton, feedback, streamingState, convRehydration, tdzRegression, projectContextLeak, urlProjectRetention, newTaskFreshSession, v3StreamProjectContext, inheritedKbFallback, automationAgentBinding, emptyPartials; LiveActivityStream; DashboardViewer; API layer; lib contexts).
- **E2E**: Playwright chromium, zh-CN locale, storageState auth, specs: auth/chat/nav/upload + helpers.
- **Golden eval**: seed_golden_test_cases + `EVAL_GATE_ENABLED` (admin model-change regression gate with parity vs champion), `GOLDEN_EVAL_MAX_ITERATIONS`.
- **QA artifacts**: `agent_qa/` (chat transcripts, API traces, e2e reports, PPTX decks, screenshots); `docs/superpowers/notes/` acceptance results.

---

## 15. Docs & Planning Archive

`docs/` (~200 files) is the design brain of the project:

- **Numbered spec family** (`docs/00_*` … `docs/09_*`): read-me-first, backend decisions, API contract, DB schema v1, event stream contract, sandbox/artifact implementation specs, MCP gateway spec, MVP scope, seed data, testing checklist, 7-layer architecture files, latest UI/runtime/datasource specs, final UI decisions (capabilities, auto-bind).
- **`docs/agents/`**: agent-harness.md (one runtime, two definition sources: DB AgentApp rows + code-backed builtins), inline-preview-design.md (gap analysis vs Claude/Manus/Kimi).
- **`docs/superpowers/plans/` + `specs/`**: 66 dated plans + ~50 specs from 2026-07-27 → 2026-08-28 — the execution timeline (see §16).
- **`docs/plans/`**: mid-July plans (pre-git history).
- Notable gaps: `docs/bug-inventory.md` exists but is **empty**; `services/erp/` + `services/intelligence/` are pyc-only shells; `agents.py` is a 17K-line mega-router.

---

## 16. Feature Timeline (July–August 2026)

**Mid-July (docs/plans/):** canonical event stream + artifacts + sandbox; agent tool wiring/builder UX; artifact redesign; DOCX inline preview; PPTX/DOCX quality overhaul; FSM verify router; SSE reasoning; unified sidebar; P0 agent reliability.

**07-27:** Auth hardening (email+password SaaS model, 15-min access + 30-day rotating refresh, revoked tokens, rate limits, Feishu/Google removed); automation Manus-parity (SSE live progress, failure notifications).

**07-28/29:** Automation quality phases 0–3; runtime agent; skill-agent UX batch; tracing/RBAC/skillscan; skill marketplace; dashboards (e2e tests, interactivity, phase-2 design, phase-3 editable, dynamic dashboards); HTTP-pool async executor.

**07-30/31:** Scheduled panel docked; conversational dashboard builder; media dashboard integration; intelligence layer; EDIA overview; ecisco-bi forecasting "true answers".

**08-03…08-07:** Multi-tenant RBAC; nightly forecast scheduler; forecast enhancement phase 1; ecisco-bi migration + upstream page; forecast analyst; midstream decision board; sidebar/MySpace restructure; forecasting accuracy overhaul; SKU-level forecasting; forecast MLOps + HITL; foundation-model forecasting; silent ecisco-bi agent; wave-0 loader framework + decision/ROI backtest; forecasting activation + decision loop.

**08-10…08-17:** Knowledge graph comprehensive implementation (catalog indexer, schema linker, NL2SQL shadow, resource router, entity graph, report recipes, depth analysis, Data Map UI); CEO morning digest; LLM routing; professional PPT generation system; automation chat artifacts fix; business semantic layer; schema-aware multi-table analysis; PPT visual diff (LibreOffice preflight).

**08-18…08-22:** Dashboard pipeline redesign; intelligent alerts; goal contract; pending-action sequence fix; forecast what-if agent tool.

**08-24…08-25:** Qwen3 Hermes tool-call parser; session-cached re-export; gap analysis implementation (parallel tool calls, agent harness wiring, MCP client, context economics, durable memory, final-answer quality floor, realtime voice, agents.py decomposition); business-data executive pipeline; qwen3 local performance; modern PPT renderer; PPT Hermes integration; live streaming progress (4 SSE event types); ecisco-bi project knowledge cache; activity feed modernization.

**08-28…08-29:** High-quality PPT (renderer default + blocking audit + market grounding); data-driven dashboard generation (DB-agnostic profiling gate); fullstack dashboard delete cascade; **CAD agent Tier 1 (Fusion 360)**; agent-gaps build (sub-agent visibility, eval loop, browser/GUI automation); golden-eval regression gate.

---

## 17. Known Issues & Gotchas

- **Mega-router**: `backend/app/routers/agents.py` is ~17K lines / 860KB — decomposition is a known follow-up (gap analysis 08-24).
- **Pyc-only services**: `services/erp/` and `services/intelligence/` retain only `__pycache__`; functionality moved elsewhere.
- **Empty bug inventory**: `docs/bug-inventory.md` is 0 bytes — bug archaeology lives in plan filenames and git comments.
- **Context window**: model context sizes matter — `qwen3.6-27b` (32K) vs deepseek-chat (64K+); per-model tool caps and compact mode exist to prevent crashes. `ZHANLU_FINAL_SPEC(/_UNIVERSAL).md` documents the SafeContextGate + Data-Pointer + Deterministic Fallback architecture.
- **DeepSeek quirk**: on long analytical queries deepseek-chat can drop into `tool_calls` with empty content — synthesis-floor fallback + `COMPREHENSIVE_DATA_*` flags exist for this.
- **Deploys**: must restart both backend and sandbox-worker (bind-mount never reloads modules); a stale worker silently runs old skill-runner code.
- **Dashboard app dirs** use underscores (`ceo_decision_center_demo`) vs hyphen URL slugs.
- **Ports**: backend 5002, frontend dev 5173 (prod WEB_PORT, commonly 8088 locally because CodeBuddy squats IPv4 8080), postgres 5400, redis 6300, minio 9400/9401.
- **Auth tokens live in localStorage** (SDK constraint) — httpOnly-cookie migration is a documented follow-up.
- **Flag convention**: project convention is flag-OFF = current/legacy behavior; many reliability features are behind flags that are OFF by default in `.env.example`.

---

## 18. How to Run

```bash
# Full stack (Docker): Postgres + Redis + MinIO + backend + sandbox-worker + sandbox images
docker compose up -d          # or: make up

# Backend alone (dev, from backend/ with .env):
#   DATABASE_URL=sqlite:///./zhanlu.db uvicorn main:app --port 5002
#   (or use the Postgres stack: make db-setup)

# Frontend dev
cd frontend && npm install && npm run dev     # proxies /api → localhost:5002

# Production-ish: build frontend, then
#   docker compose -f docker-compose.yml -f docker-compose.override.yml up -d

# Tests
make smoke                     # backend smoke
cd frontend && npm test        # vitest
cd frontend && npm run test:e2e  # playwright

# Migrations
cd backend && python -m alembic upgrade head

# Diagnostics
curl localhost:5002/healthz
curl localhost:5002/api/_db-info
```

Seed login (current checkout): `admin@zhanlu.dev` / `admin123` (JSON `{email,password}` POST `/api/apps/{appId}/auth/login`).

---

## 19. AI Engineering & LLM Systems Perspective

*An engineer's-eye review of Zhanlu as an AI/LLM system: how the agent core is
designed, what it does well, where the engineering debt is, and what to fix next.
Written from a read of the actual code (llm_service, llm_router, synexia/fsm,
compaction, answer_verification, goal_contract, harness/orchestrator, config).*

### 19.1 Architecture Assessment — Harness-over-Brain Done Right

Zhanlu follows the production-grade pattern that frontier labs converged on:
**the LLM proposes, the harness disposes.** The model is a swappable reasoning
brain; every decision passes through typed contracts and deterministic gates:

| Contract | Role |
|---|---|
| `TaskSpec` (synexia/contracts.py) | Machine-checkable goal: task_kind, entities, artifact intents, deliverable_format, requires_data |
| `PlanDAG` | Directed acyclic plan with typed nodes (skill/tool/nl2sql/sandbox/agent), dependencies, expected_output, `topo_sort()` |
| `ObservationRecord` | Per-node evidence captured during ACT — the substrate for grounding and verification |
| `ConfidenceScore` | Deterministic confidence from weighted factors (verification, data integrity, coverage, citation) |
| `ReportCardPayload` / `DeckPlan` / `SlidePlan` | Structured payloads for artifact finalize — the LLM fills data into schemas, never free-forms |

The FSM (`INIT → GOAL → CONTEXT → PLAN → GATE → ACT → OBSERVE → VERIFY →
FINALIZE → QUALITY_EVAL → DONE`) maps cleanly onto the classic
plan-act-observe-verify loop, with a GATE state (policy evaluation) and a
VERIFY state that can loop back to PLAN (bounded re-plan) — this is closer to
Letta/MemGPT-grade agent architecture than to a toy ReAct loop.

**Strongest architectural decision:** the capability router only executes
approved plan nodes, and node types are first-class (nl2sql vs sandbox vs skill
vs tool), which keeps the model from inventing execution paths.

### 19.2 LLM Integration Layer

- **Provider-agnostic OpenAI-compatible client** (`llm_service.py`):
  `call_llm` (async), `chat_completion_json_sync`, `stream_chat_completion`,
  `get_embedding`. Works with OpenAI, DeepSeek, Moonshot, and self-hosted vLLM.
- **Hierarchical model resolution** (`llm_router.py`): `EffectiveLLM` /
  `LLMEndpoint` — project pin → agent pin → user override → admin override →
  global default; locked-model handling; per-message override.
- **Failover & circuit-breaking**: `LLM_FALLBACK_PROVIDERS` chain retries on
  HTTPStatusError/RequestError; `LLM_HEALTH_ROUTING_ENABLED` picks the healthiest
  provider; `MODEL_TASK_ROUTING` routes by task type; `LLM_RESPONSE_CACHE` caches
  temperature=0 calls in Redis.
- **Model quirks handled explicitly**: `LLM_FIXED_TEMPERATURE_MODELS`
  (`kimi-k2,o1,o3,qwen3,deepseek-v4`) omit temperature; `LLM_MAX_TOKENS_HARD_CAP`
  (4096) plus per-model context-aware clamping protect small vLLM servers;
  `LLM_PARALLEL_TOOL_CALLS_ENABLED` injects `parallel_tool_calls` only for
  OpenAI-compatible models (Anthropic excluded).
- **Reasoning streaming**: `LLM_ENABLE_THINKING` requests chain-of-thought
  (vLLM qwen3 `enable_thinking=True`) and relays `reasoning_delta` SSE events —
  a nice "Live" UX touch.
- **Sandbox LLM proxy**: skill runners in containers call the LLM through a
  Unix socket proxy (`SANDBOX_LLM_PROXY_SOCKET`) — keys never enter sandboxes.
- **Semantic response cache** ("experience layer"): question classification +
  embedding + cached-response replay can short-circuit an entire LLM run — good
  for repetitive enterprise queries.

### 19.3 Context Engineering (the anti-crash core)

This is the most sophisticated area of the codebase and the direct realization
of `ZHANLU_FINAL_SPEC(_UNIVERSAL).md`:

- **Pre-flight gate philosophy**: every LLM call is preceded by context checks;
  nothing waits for an overflow to react.
- **Protected zone**: system prompt + current user message + last 6 turns +
  pending tool calls are never compressed.
- **Escalation ladder** (`CONTEXT_ESCALATION_LADDER`): compact →
  truncate tool outputs (per-model caps, e.g. deepseek-chat 24,576 chars) →
  drop oldest tool messages → fallback to a bigger-context model.
- **Compaction suite** (`services/compaction/`): microcompact (30K tokens),
  full compact (50K), auto threshold 0.8 of window, boundary messages,
  plus **context-attachment compaction** — builds task-focus, recent-files,
  verified-work, invoked-skills, work-log attachments so a compacted history
  retains *what the agent was doing*, not just a summary.
- **3-layer tool-result persistence**: per-tool 8K char cap → disk spill
  >20K → per-turn aggregate spill >80K; read_file pinned. Large query results
  are replaced with `cache_id` **data pointers** and explored via
  `data_describe / data_top_n / data_aggregate / data_filter` — the
  data-pointer pattern used by production agents (Claude artifacts, Letta).
- **DSR compact mode**: bound-data-source prompt blocks are structurally
  compressed for models with context_window ≤ 70K.

**Verdict:** context safety is production-grade and model-agnostic. This is the
single best-engineered subsystem in the project.

### 19.4 Agent Loop Reliability Engineering

- **Budgets**: per-conversation iteration budget (default 100; `AgentApp.max_call_count` overrides), thread-safe consume/refund, execute_code refunded.
- **Guardrail tripwires**: exact-failure block after 5, same-tool failure halt after 8, no-progress block after 5.
- **Retry taxonomy**: handler-level backoff on exceptions **and** result-level retry on returned `{success: False}` dicts; post-exhaustion argument reformulation (`TOOL_REFORMULATE_MAX_ATTEMPTS`) with LLM-suggested corrected args.
- **Failure classification** (`api_error_classifier.py`): 15-reason `FailoverReason` enum drives retry-vs-give-up decisions.
- **FSM pruner**: strips orphaned tool_calls (the 400-error class from interrupted streams).
- **Turn guards**: PPT / dashboard / file guards force artifact production instead of prose deflection; `PPTX_NUDGE_MAX=2` then `tool_choice` forcing; `GOAL_CONTRACT` machine-checkable criteria with bounded remediation (`GOAL_CONTRACT_MAX_FORCES=3`); `CLARIFY_SUSPENDS_TURN_ENABLED` prevents clarify-loop death spirals; deliverable phase-lock blocks artifacts until an answer-tagged dataset exists.
- **Verification stop / background review**: max 2 nudges per turn; every ~5 turns a background review pass with a memory-tool-only whitelist.
- **Quality gate**: artifacts held back below confidence 0.4 (chat) / 0.6 (automation); `SYNEXIA_QUALITY_EVAL_ENABLED` runs an LLM completeness+reflexion critique with a bounded corrective regen loop (max 2 iterations).

### 19.5 Grounding & Anti-Hallucination (data truth)

- **NL2SQL with a validation chain**: semantic resolver (metric/mapping match) → sqlglot validation + allow-list → EXPLAIN cost estimation → policy (row limits, cost caps) → read-only execution → audit events. Multiple dialects (MySQL/Postgres/SQLite) via adapters.
- **DataSnapshots are immutable evidence**: artifacts cite snapshot IDs, never live queries; checksums verified on read; TTL 90d.
- **Answer verification is layered** (`answer_verification.py`): metadata-only detection, empty results, degenerate values, placeholder text, dimension coverage vs catalog, overscope filter detection, **part-whole inconsistency**, **cross-call total drift**, category-subset checks, arithmetic consistency extraction from prose, plus optional LLM rubric eval.
- **Plausibility checks** (`ANSWER_PLAUSIBILITY_CHECK_ENABLED`) feed retry/disclose paths.
- **Citation grounding** (`citation_grounding.py`) and a hallucination guardrail gate run at finalize; grounding checks can be pinned in an agent's `evaluation_profile`.

### 19.6 Memory & Knowledge

- `memory_advanced/`: embeddings + scorer + vector store (OpenHarness-adapted); semantic dedup at cosine ≥0.85 with consolidation and lifecycle archive/promote; `AgentMemory` rows carry `content_hash`, `importance`, `ttl_days`, `usage_count`, `project_id` scoping.
- Compaction attachments preserve working context across compacts; session memory + project memory (review/edit UI) exist; KB RAG via ChromaDB with hybrid retrieval flags.
- Knowledge graph (value-chain, config-driven) + catalog indexer + schema linker feed `ask_knowledge_graph` and join-edge detection for multi-table analysis.
- **Gap**: memory is mostly implicit (auto-written). There is no user-facing memory dashboard, and importance/TTL fields are not yet driving promotion decisions in the main loop.

### 19.7 Evaluation & Model Governance

- **Offline eval pipeline** (`eval_pipeline.py`): samples conversations at `EVAL_SAMPLE_RATE` (default 0.1, cap 50/run), scores dimensions (completeness, confidence), persists + daily report. **Default OFF.**
- **Golden-eval regression gate** (`golden_eval_runner.py`, 08-29): admin model changes can be blocked (409) when a candidate fails the test-case suite vs champion parity — floors (`EVAL_GATE_FLOOR=0.8`) + parity tolerance (0.05), fail-closed per-case timeout, LLM judge with artifact-evidence checks. **Default OFF** but the `/api/admin/evals/regression` preflight works regardless.
- **Admin observability**: `admin_invocations` (status/cost/duration per invocation), `admin_evals`, `agent_metrics` (10 categories via `GET /api/_agent-metrics`), tracing + threshold alerts (guardrail halt >10, no-progress >10, budget exhaustion >20, context overflow >10, rate limit >20, fallback failures >5, sanitization repairs >50).
- **Prompt AB test** table exists (`prompt_ab_test.py`) but gated off.
- **Forecasting MLOps** is the most mature eval loop in the repo: realized-accuracy tracking, drift detection + auto-adjust, bias correction, HITL approval, staged threshold auto-tuning (requires ≥30 scored decisions and ≥45% classifier accuracy), and an **honest negative culture** (the 08-08 accuracy report openly reports MAPE 9.4%/14%/19.7% at 7/14/30d and directional accuracy worse than random).

### 19.8 Multi-Agent, Delegation & Swarm

- Sub-agents run inside parent-approved execution contexts with **delegated data grants** (none/snapshot_only/query_via_gateway/schema_only/review_only), recursion denylist, per-subagent budgets, and `delegation_nudge` heuristics.
- `harness/orchestrator.py` (`AGENT_HARNESS_ENABLED`): run store events, checkpointing, tracing, messages snapshot, hash-based arg persistence — the newest gap-closure work (08-29 commit enabled swarm tier).
- Swarm: bounded teams with mailboxes, result-driven re-dispatch + escalation, aggregated summaries — bounded by max steps/cost/runtime per the master spec.

### 19.9 What Is Genuinely Strong (differentiators)

1. **Deterministic decision layer over probabilistic prose** — the forecast BUY/SELL/WATCH decision is 100% rules; the LLM only writes the narrative. This is the correct pattern for enterprise trust.
2. **Honest-negative reporting** — confidence labels, trust tiers, and published accuracy regressions instead of marketing numbers.
3. **Artifact-first, evidence-linked outputs** — every deliverable has versions, source DataSnapshot IDs, build manifests, validation reports, preview derivatives.
4. **Governed sandbox with LLM proxy** — ephemeral containers get zero credentials.
5. **Capability abstraction** — non-technical users pick "Make Dashboard", not internal skill names.
6. **Context safety stack** — proactive gating + data pointers + deterministic fallback; genuinely production-grade.
7. **Dual-generation dashboards** — legacy widget + full-stack generated React apps, both with cascade delete, versioning, and DB-agnostic SQL guards.

### 19.10 Weaknesses & Engineering Debt (AI-system view)

1. **Mega-router** — `agents.py` (~17K lines / 860KB) hosts streaming, FSM wiring, legacy loop, session management. High regression surface; the 100+ Chat tests exist *because* of this, not despite it.
2. **Feature-flag sprawl** — 100+ flags, most **default-OFF**, meaning "behavior" is an environment matrix, not the code. Reasoning about the true production behavior requires reading .env, not the source. Flags also accrete: several are documented as "historical behavior preserved" — that is how bugs become features.
3. **Prompt drift risk** — directives are embedded in code (`_RESEARCH_ANALYST_DIRECTIVE`, `_AUTONOMY_CONTRACT`, multi-table protocols in `agent_prompts.py`, plus system_skills files). No central prompt registry, versioning, or A/B infrastructure wired to the eval loop.
4. **Evaluation is off by default** — `EVAL_PIPELINE_ENABLED`, `EVAL_GATE_ENABLED`, prompt AB test, verifier LLM are all flag-off. The machinery is excellent; the *habit* is not yet on.
5. **Heuristic-heavy verification** — deterministic detectors (part-whole, drift, coverage, overscope) are pattern-based; they need continuous calibration against false positives/negatives, and today calibration is manual.
6. **Local-LLM fragility** — qwen3 vLLM quirks (tool-call parser, fixed temperature, small windows) are handled, but each workaround adds a special case; a model-agnostic fuzz suite for tool-call parsing would de-risk new models.
7. **Dead/moved code shells** — `services/erp/` and `services/intelligence/` are pyc-only; documentation and tooling still reference them, which misleads contributors.
8. **Tenancy by convention, not enforcement** — isolation relies on RBAC rows + per-call checks; Postgres RLS exists in the schema spec but is not the enforcement mechanism.
9. **Cost/latency of multi-stage pipelines** — deck planner → router → layout → audit/repair → polish → quality eval can stack many LLM calls per deliverable; no per-pipeline budget dashboards surfaced to admins (token tracker exists; visibility is limited).

### 19.11 Recommended Next Steps (ranked)

1. **Decompose `agents.py`** — extract stream/fsm/session modules behind stable interfaces; add an ownership boundary test (import graph).
2. **Central prompt registry + versioning** — move directives into versioned prompt artifacts, wire golden-eval canary: *shadow dispatch → golden eval → limited canary → controlled automation → rollback rehearsal → freeze* before any prompt or model change.
3. **Flip eval on in staged mode** — run `EVAL_PIPELINE_ENABLED` in shadow, publish weekly dimension scores; enable `EVAL_GATE_ENABLED` in staging CI on every model/prompt change (the gate already exists — operationalize it).
4. **Consolidate flags into tiers** — document a 3-tier flag policy (T1 safety, T2 behavior, T3 experimental), add an admin config page, and purge flags older than N months that are permanently ON.
5. **Cost/latency budget per pipeline** — extend the token tracker to emit per-artifact-pipeline cost summaries and alert on budget breaches.
6. **Online quality loop** — sampled LLM-as-judge with a human review queue (admin invocations already provides the data model); feed corrections back into the golden suite.
7. **RLS for row-level tenancy** — implement the documented `audit`/`governance` + RLS extension so isolation is enforced by the database, not only middleware.
8. **Tool-call parser fuzz suite** — property tests over model outputs (deepseek, qwen3, kimi) to catch parser regressions before deployment.
9. **Resurrect or delete pyc shells** — either restore `erp/`/`intelligence/` source with documentation, or remove the shells and update references.
10. **Memory visibility** — surface agent/user memory in the UI (importance, TTL, sources) so users can prune what the agent "remembers" — enterprise trust feature.

### 19.12 Bottom Line

Zhanlu is an **above-average enterprise agent platform** by AI-engineering
standards: it has the harness-over-brain discipline, a genuinely production-grade
context-safety stack, deterministic grounding (NL2SQL + DataSnapshots +
verification), honest evaluation culture in forecasting, and governed artifact
delivery that most similar products lack. Its main risks are organizational:
a mega-router, flag sprawl, default-off eval, and prompt drift. The fixes are
mechanical and already half-built in the repo — the highest-leverage move is
operationalizing the eval gate + golden suite as a CI canary gate and
decomposing the chat router.

---

*Generated 2026-08-29 by Synexia Agent from a full read of the repository (code + docs), including an AI-engineering review of the LLM/agent core.*
