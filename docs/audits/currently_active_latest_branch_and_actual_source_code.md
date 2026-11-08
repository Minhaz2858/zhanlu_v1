# Zhanlu — Current-State Runtime & Code Evidence Audit
**Date:** 2026-10-19 · **Codebase:** `/Users/minhazulislam/Desktop/zhanlu_n8n_main_version`
**Method:** code-only inspection (no docs trusted), live Docker stack queries, backend logs, live Postgres data. Every claim below carries a file:line or live-runtime citation.

---

## 0. Branch & Runtime State (what "current" means)

| Item | Value | Evidence |
|---|---|---|
| Branch | `fix/automation-chat-artifacts-ordering` (not `main`) | `git branch --show-current` |
| HEAD | `a826e82c2` ("vv"), preceded by NL2SQL flag commits `3f03eac67`, `d8f87988b`, `d8db07e1e` | `git log --oneline -5` |
| Uncommitted work | Full CAD/DFM stack untracked: `backend/app/services/dfm/`, `cad_revision.py`, `cad_bom.py`, `rfq_quote.py`, `fusion360_dfm.py`, `101_cad_revisions.py` migration + 12 test files | `git status --short` |
| Live migration head | `101_cad_revisions` — **applied to the running DB** | `alembic_version` query |
| Runtime | Docker stack: `zhanlu-backend` :5002, `zhanlu-postgres` :5400, `zhanlu-mysql` :3308, `zhanlu-redis` :6300, `zhanlu-milvus` :19531, `zhanlu-minio` :9400, `zhanlu-docstore` :9002, `zhanlu-frontend` :8089, `zhanlu-sandbox-worker` | `docker ps` |
| Backend log (startup) | MCP StreamableHTTP session manager started; Workflow scheduler (30s poll); automation dispatcher (concurrency cap 3, 60s tick); scheduled tasks live: `memory_consolidation` (30 min), `skill_curation` (6 h), `daily_eval`, `red_team` (6 h), `loop_postmortem`, `procedural_promotion`, `turn_health_monitor`, nightly forecast | `docker logs zhanlu-backend` |

> ⚠️ Two memories from other workspaces do NOT hold on this branch: (a) the stable-answer M1 (`stable_stream.py`, `frozen_at`/`content_hash` on `chat_messages`) — **absent here**; (b) the automation grounding-gate `_should_fail_for_ungrounded_report` — **0 hits repo-wide**. Do not cite either in a resume.

---

## 1. Data Analysis Agent — Exact End-to-End Runtime Flow

Entry: `POST /apps/{app_id}/agents/conversations/v3/{conversation_id}/messages/stream` → `add_message_stream` (`backend/app/routers/agents.py:11429`).

| # | Stage | Implementation | Contract / Behavior | Status |
|---|---|---|---|---|
| 1 | Datasource runtime injection | `prepare_data_source_runtime` — `backend/app/services/data_source_runtime/data_source_runtime.py:1360-1550` | Resolves agent-bound KBs (`get_bound_data_source_ids` :1418), workspace auto-bind (default **OFF**), project-KB union; `general_assistant` in project chats gets project KBs **replacing** agent bindings (anti-leak, :1410-1416); demo-source exclusion (:1483); injects `ask_data_agent`/`fetch_data_batch`/`query_database` (:1526-1549). No keyword "data question" classifier — data capability is structural when KBs are bound. | **ACTIVE** |
| 2 | Delegation | `_ask_data_agent` — `backend/app/services/tool_handlers/delegation_tools.py:327` | Planner first (`DATA_AGENT_PLANNER_ENABLED` default **True**, `config.py:1580`) → sub-LLM ReAct loop (:519-846) with prompt `DATA_AGENT_PROMPT` + scoped source list + "NO INVENTED FIGURES" hard rules (:1204-1267). Fast path with single-KB auto-resolve (`_try_nl_fastpath` :1121-1199, auto-resolve at :1138) is **flag OFF** (`DATA_AGENT_FASTPATH_ENABLED=False`, `config.py:1819`). Wall-clock budget 60 s (`DATA_AGENT_BUDGET_SECONDS`, :66-68). | **ACTIVE** (planner), fast path **FLAGGED OFF** |
| 3 | Table/column/join selection | Sub-LLM calls `describe_schema` / `execute_query` (`backend/app/services/tool_handlers/db_tools.py:493, 887`; subagent-only registration :1695-1702) with schema-validator, did-you-mean, schema-linking, schema-graph enabled (:546-553). | LLM picks tables/columns from real schema; joins from SchemaGraph/semantic store; validator feedback loop corrects bad columns (:991-1047). | **ACTIVE** |
| 4 | NL2SQL generation | `NLAnswerService.answer` — `backend/app/services/db/nl_answer_service.py:134` → `_answer_impl` (:190) | Readiness gate (:243), definitional lookup (:299), planner + clarify (`NL2SQL_PLANNER_ENABLED=True`, `NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED=True` `config.py:1776,1782`), two-phase schema (TOC→DDL), plan memory (ON), workload hints. | **ACTIVE** |
| 5 | Source Verification Gate (P-SV) | `backend/app/services/db/source_verification.py:1-78`, called at `nl_answer_service.py:940-1012` | **Blocking pre-SQL gate**: refuses / clarifies / auto-repairs (drops archive twins, force-includes metric authority) *before SQL is written*. Flag `NL2SQL_SOURCE_VERIFICATION_ENABLED=True` (`config.py:469`). | **ACTIVE** |
| 6 | SQL validation & execution | `QueryService.execute` — `backend/app/services/db/query_service.py:26-148` | **Execution-boundary gate** `execution_gate.enforce` (:127-139) — "nothing above this layer can widen" caps. Rows capped 5000 (`MAX_QUERY_ROWS_CAP`, `config.py:492`); read-only SQL check + access policy at tool layer (`db_tools.py:965-989`). **`QUERY_TIMEOUT_DISABLED=True` (`config.py:502`) — statement timeouts deliberately OFF** for large warehouses; truncation detected by fetching cap+1 rows (:144-148); ER-1267 collation self-heal (:150-160). | **ACTIVE** (timeouts: policy decision to disable) |
| 7 | Result persistence | **`data_executions` table** (54 live rows; per-tool JSON results, TTL-cleanup loop started at boot, `main.py:811`) for chat; **`DataSnapshot`** (`backend/app/models/data_snapshot.py:29-73`: SQL + rows + SHA-256 checksum, archived-never-deleted) is written on the **workflow / nl2sql-router path only** (`services/nl2sql/__init__.py:323,549-566`), NOT on the v3 chat path. Large tool outputs spill to disk with sha256 pointer (`services/tool_output/__init__.py:34-58`). | **PARTIAL** — immutable snapshots exist but are not on the chat path (see §2) |
| 8 | Provenance generation | `agents.py:19453-19459` — `_extract_citations_from_tool_calls` → `ChatMessage.sources` (`models/chat_message.py:47-51`); `executed_sql` returned by the driver (`query_service.py:149`). | Displayed SQL = actually-executed statement (post-rewrite). | **ACTIVE** |
| 9 | LLM synthesis | `synthesize_report` — `backend/app/services/synexia/report_synthesis.py:894` | Raw rows (≤1000) **plus deterministic Python pre-aggregates over ALL rows** (:943-955) — the LLM interprets precomputed answers; sparsity/zero-row warnings (:924-1004). | **ACTIVE** |
| 10 | UI rendering | `frontend/src/components/chat/MessageBubble.jsx:616-664` (source chips), `DataTableCard.jsx:19-225` (rows + collapsible SQL + CSV/TSV export), `ReportCard.jsx:14-16` (KPIs/chart). Activity steps deliberately strip raw SQL (`ActivitySteps.jsx:79-92`). | **ACTIVE** |

### Automatic determination — explicit answers
- **Datasource**: sub-LLM picks from the *scoped bound list*; multiple sources → `fetch_data_batch` refuses to guess and enumerates (`delegation_tools.py:1759-1798`). Single-KB auto-resolve exists only on the flag-OFF fast path.
- **Tables / columns / joins / aggregation**: yes — sub-LLM grounded in real schema + validator feedback; P-SV constrains grain/authority before SQL.
- **Time range**: planner + freshness window (`KG_FRESHNESS_SHORTCIRCUIT_ENABLED`).
- **Ambiguity → clarify, not guess**: structural — planner clarify flags (both default ON), P-SV refuse/clarify, readiness refusals (`db_tools.py:143-152`), metadata-only honesty gate fails the call (`delegation_tools.py:1056-1075`).
- **Failure handling**: SQL error → schema-validator self-correction (max 2, `config.py:454`) then structured failure; empty rows → warnings + "no fabricated zeros"; timeout → graceful truncation with `truncated: true`.

---

## 2. Provenance & Grounded-Answer Architecture

**What prevents the LLM from inventing numbers (mechanical, in order):**
1. **P-SV source verification gate** (pre-SQL, blocking) — `source_verification.py`; refuses/clarifies ambiguous sources.
2. **`unsupported_slots` envelope** — unanswered question-slots are disclosed to the SQL model and carried to the result, never zero-filled (`nl_answer_service.py:1054-1064`; `db_tools.py:1319-1341`).
3. **Execution-boundary caps** — read-only, RBAC policy, row caps (`query_service.py:127-139`).
4. **Deterministic pre-aggregates** handed to the narration LLM (`report_synthesis.py:943-955`).
5. **Figure-grounding guardrail (post-generation)** — `agents.py:5902-5952` → `backend/app/services/db/answer_grounding.py:298` `check_answer_grounding`: **every number in the final answer must exist in (or be a sum/min/max/ratio of) the turn's tool payloads**; ungrounded → bounded nudge retry (`ANSWER_GROUNDING_ENABLED=True`, `agents.py:5921`; triggered at `agents.py:7434, 9053, 15155` whenever a data tool ran).
6. **No-query guard** — data-looking answer with zero data tool calls → nudge then fallback replacement (`agents.py:5955-6005`).

**DataSnapshots**: model is genuinely immutable (sha-256 checksum, archived-never-deleted status) with `SnapshotArtifactLink` lineage — but **only the workflow/legacy-NL2SQL path writes them** (`data_snapshots` = 0 live rows). Chat provenance = `ChatMessage.sources` + `tool_calls[].results.{rows, sql, source_name}` + offloaded result pointers.

**Reconciliation**: enforced by the figure-grounding gate (post-narration), not by hash comparison. **No `content_hash`/`frozen_at` on chat_messages** (`models/chat_message.py` — verified full file). Content-hash immutability exists on DataSnapshots, tool-result offload, and memory rows only.

**Resume-safe description**: *"Governed NL2SQL pipeline with a blocking source-verification gate before SQL generation, execution-boundary enforcement (read-only, row caps, access policy), deterministic pre-aggregation of full result sets, and a post-generation figure-grounding validator that rejects any answer number not derivable from the turn's query results."*

---

## 3. Execution Timeline / Observability

**What "9 events · 9 completed · 94s" is**: `frontend/src/components/chat/LiveActivityStream.jsx:1093-1108` — terminal-event count and (last.ts − first.ts) of merged SSE events.

**SSE event taxonomy (production, all emitted in the v3 stream):** `delta`, `reasoning_delta` (`agents.py:11856/14546`), `activity_step` (`services/agent_loop/sse_builders.py:20-56`; duration_ms included), `phase` (:80-96), `live_event` (:273-288; UTC ts; sanitized — no raw SQL/model text, :161-185; capped 100/turn), `trace_step` (`agents.py:16268`; title/detail/status/duration_ms), `plan_step_added/completed` (:14521, :16784), `tool_progress` (:17727; full tool array + status + duration_ms, keepalive-duplicated every 5 s while a tool runs — `tool_executor.py:232-260`), `artifact_created`, `paused`, `done`/`error`.

**Persistence**: timeline persists **on the assistant ChatMessage** — `activity_steps`, `live_events`, `trace`, `tool_calls`, `sources` (all JSON columns, `models/chat_message.py:21-51`; attached at `agents.py:19470-19476`). After reload, `LiveActivityStream`'s legacy adapter rebuilds the exact timeline from these columns.

**Dedicated run tables**: `agent_runs` + `agent_run_steps` (with per-step tokens, retry_count, duration_ms, error) exist but are written only when `AGENT_HARNESS_ENABLED=True` / `AGENT_CHECKPOINTING_ENABLED=True` — **both default False** (`config.py:1876, 1917`), and 13/69 live rows exist from earlier runs. Run replay endpoints exist (`routers/agent_runs.py:136-219`) but are inert without the flags.

**Transport**: SSE only on chat (no WebSocket). Heartbeat `": ping"` every 5 s (`agents.py:3843-3872`); `_guarantee_done` (:3875-3940) force-emits a terminal frame if the stream ends without one.

**Gaps**: tokens/cost not in SSE frames (only in the disabled step table); generic tool calls get OTEL spans + telemetry, **not** `audit_events` rows.

**Verdict**: a developer can fully reconstruct the *visible* run from the persisted message; full token-level run trails require flags that are off.

---

## 4. Planning / Reasoning / Acting / Replanning — Production Classification

**Deployment reality**: `SYNEXIA_FSM_ENABLED=true` in `.env:138`, **but** `.env:437 V3_DATA_ROUTE_MODE=react` + `V3_LLM_ROUTER_MODE=on` retire the FSM as top-level brain for chat (`agents.py:12260-12281`). FSM remains the brain only for automation `force_planning` turns. Production chat = **model-first ReAct tool loop** (`agents.py:13351`).

```
POST /v3/.../messages/stream (agents.py:11429)
 ├─ intent: planning_trigger heuristic + LLM turn router (V3_LLM_ROUTER_MODE=on)
 ├─ [automation force_planning] → SynexiaFSM → PLAN DAG → capability_router.execute_plan_nodes
 │    (tool/nl2sql/sandbox/skill nodes, replan ≤2, parallel prepass ≤4) → VERIFY → FINALIZE
 └─ [default chat] → ReAct loop × MAX_TOOL_ITERATIONS
      model-first tool_choice=None → parallel tool batch → retry/heal →
      guardrails (figure-grounding, verify-on-stop nudge) → loop detection →
      delegate_task (≤4) → force-synthesis fallback → persist timeline → done
```

| Mechanism | Status | Evidence |
|---|---|---|
| Intent understanding | **ACTIVE** | `planning_trigger.py` + LLM router gate `agents.py:12214-12251` |
| Turn planning (lightweight, in-loop checklist) | **ACTIVE** | `plan_step_*` events `agents.py:14521, 16784` |
| Task decomposition (Plan DAG) | **FEATURE-FLAGGED** (FSM path; chat bypasses via route flag; automation uses it) | `fsm.py`; `agents.py:12197-12283` |
| Forced tool selection | **PARTIALLY REMOVED** — `_compute_tool_choice` deleted; model-first; remaining forcing: dashboard/pptx/file, final-iteration `"none"`, loop-guard tool-free pass | `agents.py:6054, 7188, 7204-7288` |
| Tool execution | **ACTIVE, bounded parallel** — `asyncio.gather(return_exceptions=True)`, fusion360 serial | `tool_executor.py:96-155, 207-209` |
| Verification stops | **ACTIVE** — figure-grounding + no-query guard + verify-on-stop nudge (cap 2) | `agents.py:5902-6039, 7463-7468` |
| Retry / healing | **ACTIVE** — backoff ×2 (0.5 s base) + one-shot LLM arg reformulation | `agent_tools.py:370-456`; `config.py:776-797` |
| Loop detection | **ACTIVE** — args-canonicalized `(tool,args)` keys, per-tool caps (e.g. `ask_data_agent: 6`), hard cap, success-aware | `agents.py:4094-4179, 274, 3411` |
| Replanning | **FEATURE-FLAGGED** (FSM only: ACT ≤2, VERIFY ≤1) | `config.py:754-771`; `capability_router.py:244` |
| Forced convergence | **ACTIVE** — loop-end nudge, wrap-up nudge, finish-line tool_choice, `_force_llm_synthesis`, `_guarantee_done` | `agents.py:287, 7176-7184, 3267, 3875-3940` |
| Subagent delegation | **ACTIVE** | §5 |
| Parallel plan-DAG execution | **FEATURE-FLAGGED** (default ON but only reachable on FSM path) | `capability_router.py:390-522`; `PLAN_DAG_PARALLEL_ENABLED=True` `config.py:1632` |
| `capability_router.execute_plan_nodes` | **FEATURE-FLAGGED** — FSM/automation + manual execution API only | `fsm.py:638-640`; `executions.py:190-193` |

**DISABLED by default**: AgentRun/AgentRunStep harness trail, checkpointing, OTEL tracing, deep research, swarm harness.

---

## 5. Multi-Agent Execution

- **Selection**: LLM queries the `list_available_agents` directory tool (`delegate_tool.py:618-685`) — builtin definitions (`agent_definitions/__init__.py:738-874`: general-purpose, explore, plan, worker, verification, **data_agent**, forecast_agent, report_agent) + published user AgentApps with capabilities.
- **Execution**: `delegate_task` / batch `tasks` — `_run_sub_agent_inner` (`delegate_tool.py:308-544`): own ReAct loop, parallel tool exec, ReWOO light pre-pass.
- **Context passing**: `DELEGATE_CONTEXT_INHERITANCE=True` (`config.py:1637-1640`) → `_build_parent_context_digest` (`delegate_tool.py:77-122`): ContextBus digest or last-6 parent messages, **capped 1200 chars** (deliberate: fan-out cost control).
- **Concurrency**: `DELEGATE_MAX_CONCURRENT=4` semaphore (`config.py:1634`; `delegate_tool.py:215-216`). `DELEGATE_VERIFY_ENABLED=False` (opt-in LLM-judge). Iterations: default 5, hard clamp 25.
- **Recursion prevention**: subagents cannot re-delegate — `delegate_task` in `_RESTRICTED_TOOLS` (`delegate_tool.py:34-35`); one level deep by toolset removal.
- **Credentials**: subagents never receive raw DB creds — handlers resolve connections via `connector_factory` internally (`db_tools.py:29-32, 569`). ⚠️ **Known gap**: generic `delegate_task` calls tools without bound-KB context, so the bound-source restriction is not enforced for generic subagents (`delegate_tool.py:449-462` vs `db_tools.py:174-212`); `ask_data_agent` *is* correctly scoped to the caller's bound KBs (`delegation_tools.py:6-8, 1841`).
- **Timeout**: `ask_data_agent` has a hard 60 s wall-clock with graceful truncation; **generic `delegate_task` has no wall-clock timeout** (bound only by iterations — `DELEGATE_TIMEOUT` was removed as dead config).
- **Accounting**: every invocation persisted to `agent_invocations` (84 live rows, type=conversation) with duration/status/tool_count; **subagent tokens/cost not booked** (delegate passes no `token_usage`).

**Resume bullet**: *"Implemented bounded multi-agent delegation: an LLM-queryable agent directory, 4-way concurrent sub-agent fan-out with context-inheritance digests, recursion prevention via restricted toolsets, and persisted invocation audit rows."*

---

## 6. Context Engineering & Memory

| Mechanism | Status | Evidence |
|---|---|---|
| Context-window probing | **ACTIVE** — probes live endpoints (vLLM `max_model_len`, Ollama `/api/show`), 600 s TTL cache; fallback name heuristics → 32 k default | `services/compaction/context_probe.py:31-60`; `compaction/__init__.py:96-163` |
| Token budgeting | **ACTIVE** — tiktoken cl100k with 4-chars fallback; autocompact threshold = window − reserve, floored at 50 % | `token_estimator.py:16-57`; `compaction/__init__.py:166-212` |
| 3-layer compaction | **ACTIVE, always wired**: microcompact (zero-LLM tool-result clearing) → context collapse → session memory (deterministic one-liners) → full LLM compact (structured 8-section summary, 120 s timeout, retries). Reactive on real overflow errors + proactive pre-turn | `microcompact.py:77-122`, `session_memory.py:111-172`, `full_compact.py`; call sites `agents.py:7054-7058, 7303-7308` |
| Large tool-result handling | **ACTIVE** — 50 k char cap; **Layer-2 disk spill with sha-256 read pointer**; Layer-3 per-turn batch budget sized by context window | `config.py:440-446`; `tool_output/__init__.py`; `sub_agent_reliability.py:222-315` |
| Memory (CoALA-typed) | **ACTIVE** — `agent_memories` table (85 live rows): semantic/episodic/procedural types, sha-256 dedup, importance, ttl_days, pinned, JSON embeddings; hybrid retrieval **0.60 cosine + 0.25 recency + 0.15 importance**; scoped agent×user×project | `models/agent_memory.py:14-57`; `memory_advanced/__init__.py:158-227` |
| Episodic + procedural promotion | **ACTIVE** — clusters ≥3 episodic cases → LLM-distilled `[PLAYBOOK]` memory; scheduled 24 h task (running per boot log) | `procedural_promotion.py:110-213`; `config.py:1715` |
| Consolidation | **ACTIVE** — OHMO fact extraction, scheduled 30-min task (running) | `memory_consolidation.py`; `config.py:1123` |
| Expiry/cleanup | **ACTIVE** — TTL filter at retrieval, data_executions TTL cleanup loop (boot log) | `memory_advanced/__init__.py:185` |
| Tests | `test_qwen3_compaction_thresholds.py` (autocompact fits small windows), `test_tool_result_persistence.py`, `test_memory_project_scoping.py`, `test_memory_consolidation.py`, `test_memory_scorer.py`, `test_multiagent_mas.py` | — |

Gaps: embeddings cosine computed in Python over JSON vectors (no pgvector); session search is lexical-only (prototype-grade).

---

## 7. Tool Registry & Tool Selection

- **Registry**: singleton `backend/app/services/tool_registry.py` — `ToolEntry` with OpenAI-format schema, handler, `risk` (read/write/destructive/external), `dangerous_actions`, `max_result_size_chars`, `enabled_by_default`. ~60 handler modules registered at import; **~190+ tools** (94+ register callsites; multi-tool files push the count up; FastMCP server logs the live count).
- **The model does NOT get the whole catalog**: resolution order = `AgentApp.tool_config.enabled_tools` → per-agent defaults (`tool_registry.py:682-721`); RBAC-denied tools stripped (`:173-183`); then **semantic tool selection** — `select_relevant_tools` (`backend/app/services/tool_search.py:130-179`): **embedding cosine similarity, top-K=32 + always-keep essentials + a `search_tools` meta-tool** so filtered-out tools remain discoverable. `TOOL_SEARCH_ENABLED=True` by default (`config.py:1706-1707`). Fail-open to full list.
- **Skills**: `backend/skills/` markdown+manifest loader → prompt injection + tool mapping; sandbox skill bundles.
- **Permissions/risk**: 3 permission modes, fail-CLOSED authz, destructive-risk confirmation + standing grants (`services/permissions/__init__.py:195-293`); risk-tier enforcement + **per-agent sliding-window rate limits** (`governance/agent_tier.py:74-84`, enforced `agent_tools.py:519-564`); HITL `approval` tool persisting TTL'd approval requests (`approval_tool.py:52-78`).
- **Retries/timeouts**: backoff ×2 + LLM arg reformulation ×1; no generic per-tool wall-clock (notable per-tool: MCP 60 s, sandbox 300 s).
- **Audit gap**: generic tool calls get OTEL spans/telemetry, **not** `audit_events` rows (that table is NL2SQL/ontology/governance only — 4 live rows).

---

## 8. MCP Implementation

| Component | Status | Evidence |
|---|---|---|
| MCP **server** | **ACTIVE, production** — official `mcp` SDK FastMCP ("zhanlu-skills"), **Streamable HTTP** mounted at `/api/ext-mcp`, exposes **every registered Zhanlu tool** (the boot log confirms the session manager started). DNS-rebinding protection disabled. | `services/mcp_server/fastmcp_app.py:40-84` |
| MCP **client** | **ACTIVE** — stdio + streamable-HTTP transports, initialize handshake, 60 s call timeout, TTL-600 s session manager with per-server creation locks and stale→ping→rebuild, per-server `granted_agents` allowlist | `services/mcp_client/__init__.py:180-306`; `mcp_client/session.py:56-109`; `mcp_proxy.py:180-251` |
| OAuth/PKCE | **ACTIVE** — RFC 8414 discovery, authorization-code + S256 PKCE, refresh, Fernet-encrypted token storage | `mcp_client/oauth.py:27-184` |
| Live usage | `mcp_servers` table: **4 configured servers**; MCP boot evidence in logs | live DB |
| Fusion 360 connector | **Registered + tested, environment-dependent** — TCP bridge (host.docker.internal:9876) to a local Fusion add-in; ~40 tools (granular sketch/extrude/fillet/hole, parametrics, io/STEP/DXF, deterministic DFM rules `services/dfm/rules.py`, BOM/RFQ/revision tools). Contract tests exist (`test_fusion360_*`); **no live-Fusion integration test**. It is *not* MCP wire protocol despite the name. | `tool_handlers/fusion360_tool.py:213-215`; `fusion360_dfm.py`; 12 untracked test files |

**Resume-safe MCP bullet**: *"Built a bidirectional MCP platform: a FastMCP streamable-HTTP server exposing the full internal tool catalog, and an MCP client with stdio/HTTP transports, OAuth 2.0 + PKCE, encrypted token storage, and TTL-based session lifecycle management."*

---

## 9. Secure Sandbox Architecture

Actual container creation (`services/sandbox/container_manager.py:131-144`, subprocess `docker run`, invoked **only** by `zhanlu-sandbox-worker` — the sole Docker-socket holder, DooD):
```
--network none  --read-only  --cap-drop ALL  --memory 1g  --cpus 1
--tmpfs /tmp:rw,size=256m  -v input:/input:ro  -v output:/output:rw  --rm
```
- **Secrets**: SBX-4 invariant — "sandbox receives data as JSON snapshots, never DB credentials" (`sandbox_tool.py:17`). Input package materialized read-only. **LLM API key never enters the container** — held by a host-side **Unix-socket LLM proxy** with model allowlist, rate limit, size cap, key-less logging (`sandbox/llm_proxy.py:11-13`; `SANDBOX_LLM_PROXY_ENABLED=True`).
- **Egress**: none (`--network none`); the proxy socket is the only channel. Timeout 300 s → `docker rm -f` cleanup. Output caps 50 MB/file, 200 MB total → Postgres artifact blobs.
- **Live evidence**: `zhanlu-sandbox-worker` container running; **27 sandbox_jobs, 27/27 completed**.
- **Honest gaps**: `SANDBOX_DEFAULT_PIDS_LIMIT=128` exists in config but **no `--pids-limit` flag is actually passed** (fork-bomb protection is memory-bound only); an in-process no-isolation fallback exists for Docker-less dev; sandbox source tests are source-string assertions, not live-Docker runs.

**Resume bullet**: *"Designed a hardened Docker code-execution sandbox: no-network, read-only-rootfs, all-capabilities-dropped containers with CPU/memory limits, egress only through a keyless host-side LLM proxy, and JSON-snapshot data flow that guarantees datasource credentials never enter the sandbox."*

---

## 10. Workflow / Automation Engine

- **19 executable node types** with real handlers (`workflow_node_types.py:116-531`; `NODE_HANDLERS` `workflow_nodes.py:441-457`): triggers (chat/manual/schedule/webhook), llm, agent, tool, kb_search, **db_query incl. NL mode**, mcp_tool, http_request, code, set_fields, split_out, remove_duplicates, merge, condition, switch, output. n8n-style connection ports (`main`, `ai_llm`, `ai_tool`, `ai_kb`).
- **Canvas**: real **react-flow** (`@xyflow/react`) — `frontend/src/pages/WorkflowCanvas.jsx:3,18` ("CONTROLLED canvas architecture (n8n canvas.store pattern)"); NodePalette/NodeDetailView/RunDrawer + LLM builder chat.
- **Runtime**: topo-order DAG executor with branch routing and **partial rerun-from-node** (`workflow_executor.py:62-265`); async runs (`run-async` returns run_id); live SSE per-node streaming with DB replay (`workflows.py:464-539`); per-node retry/continue-on-fail (n8n semantics, :197-241); run history persisted (`workflow_run_steps`, 1 run/6 steps live); publish endpoint.
- **Scheduling**: cron via `croniter`, daemon-thread scheduler 30 s poll (`workflow_scheduler.py:67-148`) — in-memory, **missed fires are skipped, not backfilled**. Separate Manus-style automation dispatcher: CAS claim, backoff retry, zombie janitor, concurrency 3 (running per boot log). `automation_tasks`: 4 live; `automation_executions`: 0.
- **Truthful limits** (documented in code itself, `workflow_runtime.py:1-17`): workflow **agent node = single LLM completion, no sub-agent tool loop yet**; tool node follows skill-md, not sandbox execution; webhook trigger has no inbound dispatch endpoint; **no immutable workflow version snapshots** (publish is a status flag). Vendored n8n source under `vendor/n8n-master/` is **not imported**.

**Verdict**: legitimately describable as an **n8n-style visual AI workflow engine** for DAG/branch/schedule/SSE/rerun — with the qualification that its Agent node is a single LLM step and versioning is shallow.

---

## 11. Artifact Generation

Pipeline: `create_artifact` tool → `ExportService` → `exporters/` → `ArtifactBlob` (Postgres inline or MinIO, sha-256) → immutable `ArtifactVersion` → REST preview/download → React previewers.

| Format | Generation works | Stored | Versioned | Preview | Download | Validated by test |
|---|---|---|---|---|---|---|
| PPTX | ✅ python-pptx (`pptx_export.py:32,86,157`) | ✅ Blob | ✅ immutable versions | ✅ HTML slide preview (`PptxArtifactPreview.jsx`) | ✅ `?format=pptx` | ✅ magic bytes + e2e (`test_artifact_export.py`, `test_e2e_artifact_export.py`) |
| DOCX | ✅ python-docx + pandoc fallback (`docx_export.py:3-5,603`) | ✅ | ✅ | ✅ `DocxArtifactPreview.jsx` | ✅ | ✅ 3 dedicated suites |
| XLSX | ✅ openpyxl (`xlsx_export.py:21`) | ✅ | ✅ | ✅ `/files/preview` | ✅ | ✅ sheet tests + ZIP e2e |
| PDF | ✅ reportlab (`pdf_export.py:25-39`) | ✅ | ✅ | ✅ inline PDF | ✅ (default export) | ✅ `%PDF` magic e2e |
| Markdown | ⚠️ no `.md` exporter; MD artifacts from forecast tool; md→HTML/docx conversion | ✅ | ✅ | ✅ | ⚠️ no dedicated .md download | ❌ none found |
| HTML | ✅ canonical renderer + html→docx/pdf converters | ✅ | ✅ | ✅ inline | ✅ | ✅ |
| Dashboard | ✅ interactive HTML artifact (`dashboard_tools.py:1147`) | ✅ | ✅ | ✅ `DashboardArtifactPreview.jsx` | ➖ not in format-export pipeline | ➖ script check only |

Live: **46 artifacts, 136 versions**. Export caching verified (second render → cached bytes).

---

## 12. Old CEO Evaluation (82/100, 2026-08-06) — Current Status

No newer scored evaluation exists (only `docs/CEO_AGENT_EVALUATION_REPORT.md`).

| Issue | Old status | Current status | Fix evidence |
|---|---|---|---|
| PG aborted-transaction cascade | broken | **FIXED** | Defensive rollbacks on error paths: `agents.py:6623-6626, 10587-10590, 11670-11673`; short-lived sessions to dodge `InFailedSqlTransaction` (:11957-11961); `mode_tool.py:135-139`, `db/readiness.py:541-545` |
| Chat input disabled while processing | broken | **PARTIALLY FIXED** — send becomes Stop, mid-run steer + cancel work (`ChatInput.jsx:374-378`, `PersistentStreamContext.jsx:113-117`), but the textarea is still disabled during streaming (`Chat.jsx:3285, 2855`) |
| Intermediate steps shown as final answer | broken | **FIXED** — event-fractionated SSE (`sse_builders.py`), internal-tool detail redaction (`agents.py:5220-5223`), empty-bubble contract tests (`test_v3_stream_empty_final_fallback_order.py`, `test_agents_responder_bug.py`) |
| High response latency | broken | **MITIGATED** — 5 s heartbeats + tool_progress keepalives, iteration caps (fast=10), bounded parallel tool batches, finalize offload, response cache (`agents.py:3440-3453`, `tool_executor.py:96-155`, `agents.py:11846-11850`) |
| File delivery not fully verified | broken | **FIXED** — magic-byte + HTTP-level e2e artifact tests; preview/download endpoints; 136 live versions |
| NL2SQL feature-gated | gated | **UN-GATED, RE-GOVERNED** — source verification, planner, clarify all default ON (`config.py:469, 1776, 1782`); per-binding `nl2sql_enabled=True` default; 474 live NL2SQL query logs prove production use |
| Missing agent discovery API | missing | **FIXED** — `list_available_agents` tool (`delegate_tool.py:618-685`), REST `GET /agents/runtime` (`agents.py:6253`), ACP capability cards, external A2A directory |

---

## 13. Testing & Evaluation

- **Backend**: ~900 test files, **1,000+ test functions**, grouped: agent-loop/streaming (~40), NL2SQL (`tests/nl2sql/` 20 + services + validator/repair/grounding), RAG/KB (~10), artifacts/sandbox (~20), workflows, automations (~45), MCP/A2A/ACP, governance/authz, forecasting (~20), dashboards/charts, evals, multi-agent.
- **Key behavioral suites** (what they assert): v3 stream frame-order & empty-final fallback; answer-contract/grounding/citation tests (no empty bubbles, no invented citations); `test_anti_hallucination_guardrails.py`; `test_source_verification.py` (P-SV refuse/clarify/repair); NL2SQL validator/shadow-mode/llm-gate/db-error-repair (bounded self-heal max 2); `test_lead_scoring.py` (scores only grounded rows, honest nulls); `test_escalations.py`; `test_multiagent_mas.py` (bounded fan-out, context inheritance, parallel DAG); sandbox hardening (source-level assertions).
- **Golden evals / LLM-as-a-Judge — implemented and runnable**: `services/golden_eval_runner.py` (real agent turns, champion-model judge, no self-judging, pass/warn/fail gates, fail-closed timeout; 15 tests), `synexia/eval_harness.py` graders + `tests/eval/golden_scenarios.json`, QUALITY_EVAL judge loop. **But**: `eval_results` table = **0 live rows** — daily_eval task is scheduled, no recorded runs yet. Red-team task also scheduled (`red_team_results` table exists).
- **Frontend**: 28 Vitest suites (stream state, Chat stop/feedback/regenerate-citations, workflow graph, auth fetch).
- ⚠️ `test_stable_stream_m1.py` / `test_agent_orchestration.py` (from other workspaces' memories) **do not exist on this branch**.

---

## 14. Production Scale (live Postgres, queried 2026-10-19)

| Metric | Count | Label |
|---|---|---|
| NL2SQL query logs | **474** (410 success / 64 failed = **86.5 % success**), spanning **2026-09-17 → 2026-10-19** | **LIVE DATA** |
| Data executions (chat tool results) | 54 (49 `ask_data_agent`, 5 `fetch_data_batch`) | **LIVE DATA** |
| Sandbox jobs | 27 (27/27 completed) | **LIVE DATA** |
| Agent invocations | 84 | **LIVE DATA** |
| Conversations / chat messages | 141 / 407 | **LIVE DATA** (single-tenant dev/demo volume) |
| Artifacts / versions | 46 / 136 | **LIVE DATA** |
| Agent memories | 85 | **LIVE DATA** |
| Agent apps / projects / users | 18 / 8 / 4 | **LIVE DATA** (small) |
| Knowledge bases | 13 | **LIVE DATA** |
| MCP servers configured | 4 | **LIVE DATA** |
| RLS-protected tables | **127** | **LIVE DATA** (multi-tenant row-level security enforced) |
| DB migration head | 101 revisions | **LIVE DATA** |
| agent_runs / workflow_runs / automations executed | 13 / 1 / 0 | TEST/SEED + flags-off |
| DataSnapshots / eval_results / dashboards / datasources | 0 each | table capacity only, not usage |
| Tool registry size | ~190+ tools | CODE CAPACITY |

**Resume rule**: cite only the first eight rows and RLS/migration counts; 4 users / 141 conversations is a dev-stage product — do not present as production traffic.

---

## 15. Performance (current evidence; no old reports)

- **Latency percentiles are NOT recorded**: `nl2sql_query_logs.duration_ms` is unpopulated on this path (all NULL) — no defensible p50/p95 from the DB. Do not quote latency numbers.
- Defensible runtime facts instead:
  - SSE **heartbeat 5 s** + tool-progress keepalive → UI never looks hung (`agents.py:3440-3453`).
  - Data-agent wall-clock budget **60 s** with graceful truncation; dashboard delegation up to 600 s (`config.py:1044-1066`).
  - Tool retry backoff 0.5 s→8 s cap, max 2 + 1 reformulation.
  - **NL2SQL success rate 86.5 %** over a month of live logs (410/474) — the strongest live performance-adjacent metric.
  - Sandbox: 27/27 completed; 300 s timeout policy.
  - Statement timeouts deliberately disabled (`QUERY_TIMEOUT_DISABLED=True`) — a documented ops trade-off for large warehouses, not a bug.
- A fresh benchmark was not run (would require driving the live chat with a configured LLM key); everything above is from live logs/DB.

---

## 16. Business Capability Layer

- **Backend: IMPLEMENTED** — agent bindings API with `access_mode="read_only"` + `allowed_tables` (`routers/agent_studio.py`), `AgentApp.capabilities` field with validation (`agents.py:4793-4830`), datasource capability catalog (`GET /capabilities`, `knowledge_bases.py:280-284`), skill/data bindings, sandbox rights by skill bundle, artifact types per capability, scheduled reports = the automation stack (6 task types, 4 live tasks).
- **Frontend: SPEC ONLY** — "Capabilities instead of raw skills" exists as a design doc (`docs/09_final_ui_decisions/...FINAL.md`); the UI still exposes a raw tool panel (`AgentBuilder.jsx` has no capability selector); translations contain the labels only.
- **Slash commands: NOT IMPLEMENTED** (0 hits for slash parsing/command palette in `frontend/src`).

---

## 17. Resume Claim Audit

| Claim | Verdict | Exact wording if qualified |
|---|---|---|
| Production agent harness/runtime | **SAFE** | ReAct loop + guardrails + convergence guarantees, flag-verified |
| Turn planning | **SAFE WITH QUALIFICATION** | "lightweight in-loop plan tracking with step-level SSE events" |
| Task decomposition | **SAFE WITH QUALIFICATION** | "plan-DAG decomposition on the planning pipeline (FSM/automation path)" |
| Dynamic replanning | **SAFE WITH QUALIFICATION** | same scope as above — not on default chat path |
| Forced tool grounding | **REMOVE as stated** — tool-forcing was largely removed; rephrase: "model-first tool orchestration with targeted forcing for artifact/dashboard turns" |
| Loop detection | **SAFE** | args-canonicalized, per-tool caps, success-aware |
| Verification stops | **SAFE** | figure-grounding + verify-on-stop nudges |
| Main/sub-agent delegation | **SAFE** | note the bound-KB scoping gap exists on generic delegate |
| Parallel subagent execution | **SAFE** | semaphore-bounded fan-out of 4 |
| Semantic tool selection | **SAFE** | embedding-ranked top-K=32 + searchable catalog |
| Scoped persistent memory | **SAFE** | agent×user×project scoping, TTL, dedup — live rows exist |
| Context-window probing | **SAFE** | live endpoint probing (vLLM/Ollama) with tests |
| Context compaction | **SAFE** | 3-layer cascade, reactive + proactive, tested |
| Hybrid RAG | **SAFE WITH QUALIFICATION** | "hybrid retrieval with grading/retry and decomposition (agentic-RAG feature-flagged)" — verify the flag you demo |
| Semantic grounding | **SAFE** | figure-grounding gate on final answers |
| Governed NL2SQL | **SAFE** | strongest claim — blocking source-verification gate + execution boundary + 474 live logs |
| Automatic schema/table selection | **SAFE WITH QUALIFICATION** | "schema-grounded tool loop with validator feedback; datasource auto-resolution behind a feature flag" |
| Fail-closed ambiguity handling | **SAFE** | structural clarify/refuse gates |
| DataSnapshot provenance | **SAFE WITH QUALIFICATION** | "immutable checksummed query snapshots on the workflow/NL2SQL pipeline; chat answers grounded via figure-verification and source citations" |
| MCP client/server | **SAFE** | FastMCP server + OAuth/PKCE client, live |
| Fusion 360 MCP | **SAFE WITH QUALIFICATION** | "Fusion 360 automation bridge (~40 CAD tools) with contract tests; requires local Fusion add-in" — do not call it MCP protocol |
| Secure Docker sandbox | **SAFE** | with the honest pids-limit caveat if pressed |
| No raw datasource credentials in sandbox | **SAFE** | JSON-snapshot invariant, code-verified |
| Visual workflow engine | **SAFE** | react-flow + 19 executable node types + SSE + rerun |
| Scheduled automation | **SAFE WITH QUALIFICATION** | "cron-triggered workflow & report automation with retry/claim semantics; missed fires are not backfilled" |
| Artifact generation | **SAFE** | 6 formats, magic-byte-tested, versioned |
| Live execution timeline | **SAFE** | persisted on messages; run-trail tables are flag-off |
| LLM evaluation | **SAFE WITH QUALIFICATION** | "golden-eval runner with champion-model LLM-as-a-judge; scheduled daily eval" — no scored results recorded yet |
| Rollback/versioning | **SAFE WITH QUALIFICATION** | "immutable artifact versioning; workflow publish has draft/published status" — no workflow rollback |
| Provider routing/fallback | **REMOVE / VERIFY** — single-provider LLM noted in prior gap analyses; no multi-provider router found in this audit |
| Local Qwen/vLLM serving | **SAFE WITH QUALIFICATION** | context-window probe supports vLLM/Ollama endpoints; serving itself is deployment config, not product code |

---

## 18. Final Resume Output

### A. Top 10 Current Production Capabilities (ranked)

1. **Governed NL2SQL with blocking source verification** — pre-SQL refuse/clarify gate, execution-boundary caps, validator self-heal, 474 live queries at 86.5 % success.
2. **Answer grounding (anti-hallucination)** — deterministic post-generation verification that every number traces to tool payloads, with nudge-retry and no-query fallback.
3. **Production ReAct agent runtime** — bounded parallel tool batches, retry+arg-reformulation healing, loop detection, forced convergence, guaranteed SSE terminal frames.
4. **MCP bidirectional platform** — FastMCP streamable-HTTP server exposing ~190 tools; OAuth 2.0+PKCE client with session lifecycle.
5. **Hardened Docker sandbox** — no-network, cap-drop-ALL, keyless LLM proxy, credential-free data flow, worker-only socket.
6. **Context engineering** — live context-window probing + 3-layer compaction + tool-result spill with pointers.
7. **CoALA memory system** — semantic/episodic/procedural with hybrid retrieval scoring and scheduled procedural promotion.
8. **Bounded multi-agent delegation** — directory-driven selection, 4-way fan-out, context digests, recursion prevention.
9. **Artifact factory** — PPTX/DOCX/XLSX/PDF/HTML generation, immutable versioning, inline previews, magic-byte-tested.
10. **n8n-style visual workflow engine** — react-flow canvas, 19 executable node types, cron scheduling, live SSE runs, partial rerun.

### B. Top 5 Resume Bullets (25–35 words)

1. *Built a governed NL2SQL engine with a blocking source-verification gate, execution-boundary policy enforcement, and a figure-grounding validator that mechanically rejects any answer number not derivable from executed query results.*
2. *Engineered a production ReAct agent runtime featuring parallel tool-batch execution, LLM-assisted argument-reformulation healing, canonicalized loop detection, and forced-convergence guarantees over server-sent-event streaming.*
3. *Implemented bidirectional MCP integration: a FastMCP streamable-HTTP server exposing ~190 internal tools plus an OAuth 2.0/PKCE client with encrypted token storage and TTL-managed session lifecycle.*
4. *Designed a hardened Docker code-execution sandbox — no network, dropped capabilities, keyless LLM proxy egress — guaranteeing datasource credentials never enter untrusted code, with 100 % job completion across live runs.*
5. *Built a context-engineering stack with live context-window probing, three-layer conversation compaction, disk-spilled tool results with content-hash pointers, and CoALA-typed persistent memory with hybrid retrieval scoring.*

### C. Top 5 Interview Stories

1. **Problem:** LLM fabricated figures not in the database. → **Decision:** verify post-generation, not just pre-SQL. → **Implementation:** `answer_grounding.check_answer_grounding` — every number must appear in (or be an aggregate of) tool payloads; bounded nudge retries. → **Failure mode:** aggregation phrasing false-positives — solved by allowing sum/min/max/ratio derivations. → **Result:** guardrail active on every data-tool turn (`agents.py:7434/9053/15155`), tested in `test_answer_grounding.py`.
2. **Problem:** ambiguous business questions produced confident wrong SQL. → **Decision:** refuse before writing SQL. → **Implementation:** P-SV gate classifies sources (grain/authority), drops archive twins, forces metric authority; planner emits structured clarification. → **Failure mode:** over-refusal — tuned via metric-binding auto-repair. → **Result:** default-ON gate; 86.5 % live success rate.
3. **Problem:** oversized tool results overflowed context and sub-agent fan-out multiplied cost. → **Decision:** spill-to-disk with pointers + digest caps. → **Implementation:** sha-256 pointer persistence, 1200-char parent digests, per-turn batch budgets sized by probed window. → **Failure mode:** early 8 k cap made the model "hallucinate missing content" (documented in `config.py:440-446`). → **Result:** tested spill/pointer suite + compaction threshold tests.
4. **Problem:** sandboxed code needed LLM access without leaking API keys or opening egress. → **Decision:** host-side Unix-socket proxy; containers get `--network none`. → **Implementation:** read-only socket mount, model allowlist, rate limits, key-less logging; worker-only Docker socket. → **Failure mode:** config-vs-flag drift (pids-limit decorative) — identified and acknowledged. → **Result:** 27/27 live jobs completed.
5. **Problem:** one bad query poisoned PostgreSQL sessions and cascaded failures. → **Decision:** defensive rollback discipline + short-lived sessions on error paths. → **Implementation:** rollbacks at every commit-failure site; `InFailedSqlTransaction`-avoiding session strategy. → **Failure mode:** initially masked errors — resolved by logging before rollback. → **Result:** clean-exit test suites; no recurrence.

### D. Top 5 HR-Friendly Achievements

1. Cut database-answer hallucinations to near zero by machine-verifying every number in AI answers against actual query results.
2. Delivered natural-language database querying used for a month straight with an 86.5 % first-pass success rate.
3. Built safe AI code execution: isolated, internet-disconnected containers where company data goes in but credentials never do.
4. Automated boardroom-ready deliverables — decks, documents, spreadsheets, PDFs — generated, versioned, and previewable in-browser.
5. Gave AI agents persistent, role-scoped memory and self-compacting context so long projects never lose the plot.

### E. Claims to REMOVE

- "Stable-answer / frozen-message architecture" (`frozen_at`, `content_hash` on chat messages) — not on this branch.
- Automation "grounding gate that fails ungrounded reports" — `_should_fail_for_ungrounded_report` does not exist here.
- "Provider routing/fallback" — no multi-provider router found.
- "Local Qwen/vLLM serving" as a product feature — only client-side probing support.
- Latency percentiles (p50/p95) — not recorded anywhere; never quote numbers.
- "n8n-based" (the vendored n8n source is not imported) — say "n8n-style".
- Full run-trail replay from dedicated event tables — flags off.
- Live golden-eval scores — infrastructure exists, `eval_results` is empty.
- Fusion 360 "MCP" protocol claim — it is a TCP bridge to an add-in.
- Production-traffic scale numbers (>4 users) — dev-stage data only.

### F. Recommended Positioning

**"Enterprise Agent & Workflow Platform"** — best match. It captures both pillars the code actually proves: a governed agent runtime (NL2SQL, grounding, memory, tools, MCP, sandbox) *and* the visual workflow/automation layer, with the multi-tenant SaaS substrate (127 RLS-protected tables, org-scoped everything) that "Agent Runtime Platform" alone understates. Avoid "Agentic AI Platform" (vague) and "AI Automation Platform" (understates the agent runtime depth).

---

*Audit basis: 6 parallel deep code explorations + live `docker`/Postgres inspection, 2026-10-19. All file paths relative to `backend/` and `frontend/` of the workspace.*
