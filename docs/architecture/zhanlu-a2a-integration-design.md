# Zhanlu A2A Integration Design (P1, Part C)

**Date:** 2026-10-31 · Companion to the GA audit (Part A). Protocol reference: IBM Think "What is Agent2Agent (A2A)?" (Google, April 2025 → Linux Foundation, June 2025). **Design only — no implementation. Preserves the ReAct runtime and workflow DAG.**

---

## 1. Headline finding

**Zhanlu already implements most of the A2A protocol — on both sides.** The P0 audit's "A2A outbound real" understated it: Zhanlu is a functioning A2A **server** for its own published agents and a functioning A2A **client** to registered external agents, plus an IBM **ACP** adapter sharing the same task store. What's missing is streaming on the inbound surface, invoke-time discovery, OAuth on the outbound path, and file artifacts.

## 2. Protocol requirements vs Zhanlu today

| A2A concept (IBM/Google spec) | Zhanlu implementation | Status |
|---|---|---|
| **Agent Card** (JSON metadata: name, description, version, endpoint, modalities, auth schemes) | `GET /a2a/agent-card` + `GET /.well-known/agent.json` — aggregated card, **one skill per published AgentApp** (`build_platform_card`, `services/a2a/agent_card.py:49-68`; `routers/a2a.py:92-101`); auth `{schemes:["bearer"]}` | ✅ Implemented |
| **Discovery** | Server: card endpoint public by design (`a2a.py:13-16`). Client: card fetch **only via manual probe** (`POST /external-agents/{id}/probe`, tries `card_url` then `/.well-known/agent.json`, extracts skills → `ExternalAgent.capabilities`, `external_agents.py:146-208`) | 🔶 Server ✅ / Client ⚠️ manual-only |
| **Task lifecycle** `submitted→working→input-required→completed/failed` (+canceled) | `A2aTask` model with exactly these states (`models/a2a_task.py:8-15`); JSON-RPC `tasks/send|get|cancel` (`a2a.py:117-206`); inline dispatch so `tasks/get` settles immediately | ✅ Implemented |
| **Messages / Parts** (TextPart/FilePart/DataPart, agent/user roles) | Text parts only; assistant message + `reply` text artifact persisted (`a2a/tasks.py:245-254`); **no FilePart/DataPart, no binary artifacts** | 🔶 Partial |
| **Artifacts** | `reply` text artifact in `A2aTask.result_json`; **no incremental artifact streaming, no files** | 🔶 Partial |
| **Streaming (SSE)** | Outbound client: `send_task_streaming` consumes remote SSE, returns final task, counts progress events (`a2a/client.py:146-228`). Inbound: **in-process event bus exists (`tasks.py:21-44`) but NO endpoint consumes it** — `POST /a2a` and ACP runs are request/response only | 🔶 Client ✅ / Server ❌ |
| **Push notifications** (webhook for long tasks) | Not implemented | ❌ |
| **Authentication** | Server: `X-API-Key` vs `A2A_API_KEY` → machine identity, or user JWT (`a2a.py:40-60`). Client: per-call Bearer token + identity headers (`client.py:26-32`); **no OAuth outbound** (OAuth exists only for MCP) | 🔶 Partial |
| **Authorization** | Server: canary gate `rollout_allows` (`a2a.py:159-164`, `agent_lifecycle.py:207-211`); ExternalAgent `granted_agents` scoping | ✅ Implemented (beyond spec) |
| **Executor** | Pluggable `A2aExecutor` registry; `DefaultA2aExecutor` runs the governed ReAct sub-loop (`_run_sub_agent_inner`) with an LLM provider ladder; **empty response = explicit `failed`, never canned success** (`a2a/tasks.py:144-257`) | ✅ Implemented |
| **ACP (IBM BeeAI alternative)** | `routers/acp.py`: `GET /acp/agents`, `POST /acp/agents/{id}/runs` (`?wait=true`), runs/messages/cancel — reuses A2A task store/executor/event bus | ✅ Implemented |
| **MCP (complementary)** | Full 2026-spec client (stdio/streamable-http, OAuth+PKCE, token refresh encrypted at rest) + FastMCP server exposing the whole ToolRegistry at `/api/ext-mcp`; per-server-tool native tools `mcp__<server>__<tool>` | ✅ Implemented |

**Also verified absent:** no OAuth on the A2A outbound path; no `tasks/sendSubscribe`-style inbound streaming; no binary/file artifact transfer; no invoke-time auto-discovery; `mcp`/`mcp_oauth` generic tool handlers are documented skeletons (real paths are `mcp_proxy.py` + `mcp_client/oauth.py`).

## 3. Where A2A vs internal delegation (decision rule)

**Internal cross-agent coordination stays on `delegate_task`.** Rationale:
- Internal delegation is same-process, shares the governed tool registry, permission checker, wall-clock budgets, session-isolated DB sessions, and the parent-context digest — none of which should cross an opaque protocol boundary.
- A2A treats agents as **opaque** (no memory/tools exposure) — correct for external partners, wrong for intra-platform agents where observability (invocation tree, P0-5 linking) and evidence grounding depend on shared context.
- The IBM article's own example matches this split: internal agent-to-database = MCP; agent-to-external-partner = A2A.

**Use A2A for:** supplier/partner/dealer-facing agents (supply-chain scenario D-class), cross-company collaboration, customers exposing their own agents to Zhanlu, and Zhanlu agents being consumed by external platforms (the server side already enables this — `agent-card` lists every published AgentApp as a skill).

## 4. Gap-closing designs (incremental, on the existing code)

### 4.1 Inbound streaming (server)
Consume the existing in-process event bus (`a2a/tasks.py:21-44`) with one new endpoint: `GET /a2a/tasks/{id}/stream` (SSE) emitting task snapshots on `publish`. The executor already transitions states; the bus already carries them. ~1 endpoint + subscribe/unsubscribe wiring in the generator's finally. Optional `tasks/sendSubscribe` JSON-RPC method mapped onto the same stream for spec parity.

### 4.2 Invoke-time discovery (client)
Before `invoke_external_agent` executes against a registered ExternalAgent whose card was probed > N hours ago, refresh the card (reuse the probe logic, `external_agents.py:146-208`) and store refreshed skills — so the parent LLM's discovery data stays current without manual probes. Flag-gated (`A2A_AUTO_DISCOVERY_ENABLED`, default off).

### 4.3 Artifact typing (both sides)
Extend `A2aTask` artifacts beyond text: accept `DataPart` (structured JSON) and file references (store via the existing file/artifact storage, reference by URL) in the executor's result handling. Keep it backwards-compatible (text-only senders unaffected).

### 4.4 Outbound auth hardening
Add OAuth2 client-credentials as an `ExternalAgent.auth_type` option alongside bearer (OAuth plumbing patterns already exist in `mcp_client/oauth.py` to reuse). Agent cards advertise the accepted scheme; the probe stores it.

### 4.5 Push notifications (defer)
Webhook push for multi-hour tasks: only when a real long-running external integration demands it (the 600 s internal wall-clock makes this rare in-platform). Register the design point, don't build speculatively.

### 4.6 Fronting GA as an A2A-visible agent
The aggregated card lists published AgentApps as skills. Decide whether `general_assistant` itself should appear (an external party dispatching "ask anything" to Zhanlu). Recommended: expose a dedicated `enterprise-assistant` skill mapped to the GA conversation pipeline only when outbound federation is actually needed — otherwise external callers should address specific specialists (least surprise, cleaner authz).

## 5. Security & isolation requirements

- Card endpoint stays public (metadata only — no data, no prompts; already the case).
- Every task dispatch remains behind JWT/API-key + canary (existing); add per-ExternalAgent rate limits before any production federation.
- External results are **untrusted input**: they enter the parent loop as tool payloads and are covered by the grounding guardrail as opaque text (same as delegated children) — preserve this invariant when artifact typing lands (a DataPart's numbers are still tool-payload figures).
- Cross-org federation must never bypass tenant RLS: external callers authenticate to a machine identity or user JWT — org context resolution follows the same `tenant_context` rules as any request (no header-trust regression).
- `ExternalAgent.auth_token` stays encrypted at rest and never serialized (`to_dict` already excludes it) — extend the same rule to OAuth tokens when 4.4 lands.

## 6. Test & acceptance criteria

- **A1:** external A2A client can fetch `/.well-known/agent.json`, see published manufacturing agents as skills, dispatch `tasks/send`, and receive a grounded completed task (existing behavior — lock with an integration test).
- **A2:** `GET /a2a/tasks/{id}/stream` emits state transitions live and terminates on terminal state (new, 4.1).
- **A3:** invoke-time card refresh updates `ExternalAgent.capabilities` without manual probe (4.2, flag-gated).
- **A4:** a remote agent returning structured JSON artifacts round-trips through `tasks/get` (4.3).
- **A5:** unauthorized caller (bad key/JWT) gets the existing JSON-RPC error; canary-bucketed caller gets `-32001` (lock existing behavior).
- **A6:** internal scenarios (Doc B, T2) do NOT route through A2A — delegation stays in-process (guard test: no `/a2a` calls in internal delegation paths).
