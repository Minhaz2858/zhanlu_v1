# Agent Gaps Build — Sub-agent, Eval, Browser (2026-08-29)

Design for closing the three gaps vs Kimi/Claude/Codex identified in the
competitive audit. Verified against the live codebase + DB (2026-08-29):
most of GAP 1 and GAP 2 are already BUILT and merely gated or unobservable.

## Audit corrections (vs 2026-08-28 audit)

- The eval table is `eval_results` (model: app/models/eval_result.py, __tablename__
  "eval_results") and it EXISTS with 0 rows. The old audit said "eval_records does
  not exist" — wrong table name. Persistence layer is complete; only the flag blocks it.
- delegate_task is a real, registered tool (services/tool_handlers/delegate_tool.py):
  isolated sub-conversation, restricted toolset (no delegate_task/memory/send_message),
  batch mode with asyncio.gather, reliability layer (sub_agent_reliability.py),
  iteration budgets, guard controllers. General_assistant + general-purpose (tools=None)
  both have it. Swarm stack (coordinator/mailbox/orchestrator/runtime) also exists.
- swarm_tools.py registers create_team/spawn_agent/send_message/get_messages/
  list_teams/scratch/orchestrate. swarm_mailbox_messages table exists.

So the real gaps are: (1a) no agent_invocations rows (observability), (1b) sub-agent
activity invisible in the chat UI (one opaque delegate_task frame), (1c) weak models
won't voluntarily delegate → no nudge, (2) EVAL_PIPELINE_ENABLED=False + no golden
seeds + no admin surface, (5) no browser/GUI automation anywhere.

## Phase 0 — Eval/regression loop (smallest)

- Set EVAL_PIPELINE_ENABLED=True in backend .env + app/config.py default.
- Verify scheduled_tasks daily_eval cycle runs (already registered, line 679).
- Seed golden test cases into agent_test_cases (5-8 cases: dashboard build, docx
  generation, SQL query, greeting, delegation) with expected verdict/dimensions.
- Add read-only admin endpoint GET /api/admin/evals (list, pass-rate by dimension,
  per-model breakdown) + GET /api/admin/evals/summary. RBAC: admin only.
- Tests: eval_results persistence, endpoint shape, scheduled task gating.

## Phase 1 — Delegation hardening (the real GAP 1)

1A. Invocation recorder: new app/services/agent_invocations.py with
    record_invocation(db, *, conversation_id, agent_name, sub_agent, duration_ms,
    token_usage, cost_amount, trace_id, confidence, tool_count, status) → writes
    agent_invocations (schema exists). Call from: delegate_tool (per sub-agent),
    main loop done frame (per conversation). Also emits per-conversation cost.
1B. Streaming: delegate_tool accepts context["stream_progress"] callback (the v3
    loop already has _emit_tool_progress_while_waiting + _tool_progress plumbing).
    Emit {"type": "sub_agent", "task", "agent", "status": running|done|failed}
    frames per subtask so the UI can render sub-agent cards.
1C. Nudge: dashboard_turn_guard-style helper delegate_task_should_nudge(user_content)
    — deterministic heuristic: user asks for N>=3 independent items (list/compare/
    analyze X, Y, Z patterns) and no delegate_task in executed names → inject a
    nudge prompt before the next LLM call ("call delegate_task(tasks=[...])").
1D. Frontend: SubAgentCard component in the chat stream — subscribes to sub_agent
    events, renders per-task status pills + final summary. Minimal (no framework
    changes; reuse the tool_call event channel).

## Phase 2 — Browser/GUI toolset (greenfield)

- Playwright Python installed into zhanlu-sandbox-worker (Python 3.11 present).
- New app/services/browser_agent/browser_mcp.py: exposes navigate/click/type/
  screenshot/extract as MCP-style tools over the existing MCP client call path
  (routers/mcp.py /mcp/tools + /mcp/call) — a built-in "Browser" toolset per app,
  NOT a separate process (MCP protocol reuse).
- Domain allowlist per app (app settings table or env): browser tools refuse URLs
  outside allowlisted hosts. No password field auto-fill. Screenshot stored as
  artifact + tool_call frame for audit.
- Agent prompt: HARD RULE "web UI task (ERP/legacy portal) → Browser tools;
  screenshots are ground truth".
- E2E: walk an ERP-ish login → query flow against a local test page in sandbox.

## Testing

Per phase: backend unit tests + frontend component test (if harness exists) + one
E2E. All deterministic; no real LLM in unit tests (fake llm_fn where needed).

## Order

Phase 0 → Phase 1 (1A, 1B, 1C, 1D) → Phase 2. Each phase = backend + UI + tests,
restart backend, verify live.
