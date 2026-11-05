# Zhanlu Enterprise Orchestration — P1 Roadmap (Part D)

**Date:** 2026-10-31 · Companion to Parts A (GA audit), B (discovery & routing), C (A2A design). Feeds off the P0 roadmap (already delivered 2026-10-31). Every item names existing code it extends. **No new orchestrator, no second registry, no rebuilds.**

---

## P1.0 — Quick wins (days; zero-risk, outsized effect)

| # | Item | What | Why now | Deps |
|---|---|---|---|---|
| Q1 | **Discovery tool into GA's toolset** | Add `list_available_agents` to the general_assistant seed list (`system_agents.py:378-403`) | GA currently cannot see the agents it is supposed to coordinate — the single cheapest unlock | None |
| Q2 | **Agent-node dispatch in the plan DAG** | Implement the declared `"agent"` node type (`capability_router.py:441-443`) via the existing `_run_sub_agent_inner` | Declared-but-dead code; gives dependency-ordered multi-specialist execution inside the existing DAG executor | Q1 not required |
| Q3 | **Child-result contract surfacing** | Parent-loop code check: `success:false` / `truncated:true` must be explicitly surfaced to the parent prompt (no LLM judge needed) | Today silent child failure is trusted to LLM re-planning | None |
| Q4 | **Unblock provider quota** | Fund a DashScope account or add a funded `llm_models` row; rerun AV smoke + PILOT_E2E golden suite | P0 exit criterion completion is blocked on billing, not code | External action |

**Acceptance:** GA answers "who can help with X" by discovery, not guessing (Doc B T1); plan-DAG agent-node test; a truncated child is visible in the answer (T4).

## P1.1 — Department context (1–2 weeks)

| # | Item | What | Why | Deps |
|---|---|---|---|---|
| D1 | `department` column on `projects` (+ entity API exposure) | Single migration; departments are labels on the existing container | The org hierarchy needs one concept, not a new entity; generic CRUD carries it free | None |
| D2 | **Context Resolver** | Extend per-message project resolution (`llm_router.py:92-139`) with department→accessible-project-set expansion for **discovery only**; "All Departments" = `project_id=None` + full accessible set | Users must not pick agents; context must scope who is discoverable | D1 |
| D3 | **Enriched `list_available_agents`** | Payload gains `department`, `home_project`, `kb_names`, portfolio lifecycle/KPI (join `AgentPortfolioEntry`), optional server-side filters | Capability-based routing needs richer catalog data at decision time | D1 |
| D4 | GA prompt routing layer | ROUTING PROCESS section in the hardcoded GA prompt (`agent_prompts.py:1767+`): decompose → discover → delegate briefs → aggregate → validate | The planner is a discipline, not a service — keeps one orchestrator | Q1, D3 |
| D5 | UI context chips | All Departments / Department / Project selector in Chat header (extends the URL-param mechanism, `Chat.jsx:474-479`); no agent picker | The product vision: users never select agents | D2 |

**Security gates:** department expansion is discovery-scoped only; data authorization stays per-child (KB bindings + `ResourceAccessPolicy` + RLS); tenant-scoping tests extended (`test_entity_tenant_scoping.py`, `test_tenant_rls.py`) — Doc B T3/T6.

## P1.2 — Multi-specialist coordination (2–3 weeks)

| # | Item | What | Why | Deps |
|---|---|---|---|---|
| M1 | **Per-task agent in `delegate_task` batch** | Optional `agent` field per task in `tasks=[…]` (`delegate_tool.py:203-211`) | One-call heterogeneous fan-out (Sales ∥ Finance ∥ Manufacturing); existing gather/semantics/walls unchanged | Q2 |
| M2 | Scenario harness (A/C/D) | Golden multi-agent scenarios as tests: production shortage, quality investigation, monthly report — asserting ≥2 departments coordinated, grounded figures, explicit partials | The acceptance criterion is cross-department coordination, not single turns | Q1–Q3, D1–D4, M1 |
| M3 | Parent-child tree + evidence affordance in UI | Render existing sub-agent `activity_step` pairs as a task tree (`LiveActivityStream.jsx`); expose persisted tool payloads as "sources" (`Chat.jsx:2812-2813`) | Trust: users must inspect execution evidence (P0 acceptance criterion, extended) | M1 |
| M4 | Metric backing for profitability questions | `MetricDefinition` becomes the ontology `metric` backing (approved ontology PRD P5; reuses NL2SQL gates) | "Highest profit" is cross-domain arithmetic — one governed definition beats agent-by-agent guessing | PRD P3a/P4 sequencing |

## P1.3 — External federation via A2A (2 weeks, flag-gated)

| # | Item | What | Why | Deps |
|---|---|---|---|---|
| A1 | Inbound SSE | `GET /a2a/tasks/{id}/stream` consuming the existing event bus (`a2a/tasks.py:21-44`) | Long-running external tasks; spec parity | None |
| A2 | Invoke-time card refresh | Auto re-probe stale ExternalAgent cards before dispatch (`A2A_AUTO_DISCOVERY_ENABLED`, off) | Discovery data freshness without manual probes | None |
| A3 | Artifact typing | DataPart/file references in `A2aTask` results (backwards-compatible) | Structured results from partners | None |
| A4 | OAuth outbound | Client-credentials option on `ExternalAgent` (reuse `mcp_client/oauth.py` patterns) | Enterprise partner auth | A2 |
| A5 | Federation guardrails | Per-ExternalAgent rate limits; untrusted-external-result handling documented (grounding covers payloads) | Production readiness before real partners | A1–A3 |

Push notifications (webhooks): **deferred** until a real long-running integration demands it — 600 s internal walls make it rare.

## Dependencies (graph)

```
Q4 (billing) ──────────────► P0 exit complete (AV smoke + live golden)
Q1 ─┬─► D3 ─► D2 ─► D5(UI)
Q2 ─┼─► M1 ─► M2 (scenario harness) ─► P1 exit
Q3 ─┘     └─► M3
D1 ─► D2 ; M4 (PRD P5) independent
A1–A5 independent track (flag-gated)
```

## P1 exit criteria

1. **Ultimate-question test:** an enterprise user, in one conversation, asks a cross-department question (Doc B Scenario A or D); GA decomposes, discovers specialists via capability data, delegates to ≥2 published agents from different departments in parallel, surfaces partials honestly, and the final answer passes the figure-grounding guardrail — with the full execution tree visible in the UI and invocations linked in observability.
2. **Isolation:** all tenant/RLS/policy suites green; department expansion provably discovery-only.
3. **No architecture violations:** still exactly one execution runtime (ReAct) + one DAG executor + one delegation mechanism + one discovery mechanism (extended, not duplicated).
4. Provider quota resolved (Q4) — all live evidence reproducible.

## Explicit non-goals for P1

No Department entity beyond a label; no embedding-based agent matcher (P2 option); no swarm/harness enablement; no second agent registry; no replacement of delegate_task with A2A for internal routing; no agent-specific hardcoded schemas or vocabulary (standing house rule).
