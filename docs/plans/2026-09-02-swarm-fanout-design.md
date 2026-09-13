# Parallel Swarm Fan-Out Pool — Design

> **Date:** 2026-09-02
> **Status:** Approved (user: "do it" — roadmap item 6 design, `docs/superpowers/plans/2026-09-02-modern-agent-gap-roadmap.md`)
> **Scope:** bounded fan-out executor for `swarm_orchestrate`. Kimi-class 300-way parallelism is a stretch goal; correctness + budgets first.

## Decisions

- **`FanOutPool` (new `swarm/pool.py`).** Runs `OrchestratedTask` items concurrently behind an asyncio semaphore, with two global budgets: `max_total_cost_usd` (cost ledger) and `max_wall_time_s` (wall clock). Budgets gate **new spawns only** — in-flight tasks always complete; skipped tasks get a synthetic `OrchestratedResult(success=False, error="skipped: <budget>")` so the lead's summary still accounts for them.
- **Reuse the retry/escalation policy.** Extract `run_task_with_retries(team_id, task, policy, runner)` as a module-level helper in `orchestrator.py`; sequential `SwarmOrchestrator.orchestrate` calls it in a loop (behavior unchanged), the pool calls it concurrently. No duplicated policy logic.
- **Cost accounting.** The pool accepts an injectable async `cost_fn(task, result, attempts) -> float` (deterministic in tests). Default: delta of `token_tracker.get_total_usage(conversation_id).total_cost_usd` between task start/end, keyed by `conversation_id`. Enforcement only active when `max_total_cost_usd > 0`.
- **Progress events.** The pool takes an async `progress_callback` seam — `pool_started`, per-task `task_started`/`task_finished`/`task_skipped`, `pool_finished`. The `swarm_orchestrate` tool returns the collected per-worker timeline in its result dict (rendered in the chat timeline after the tool call); per-task outcomes already go to the lead's mailbox via `post_summary`. True mid-run SSE streaming into `activity_step`/`tool_progress` frames is a Phase 2 router-level hook — the callback is the seam (tool handlers have no streaming channel today).
- **Wiring.** `_handle_orchestrate` (swarm_tools.py) opts into the pool: builds `FanOutPool` from settings and posts the aggregated summary to the lead mailbox on completion. Sequential `orchestrate` stays for direct callers.
- **Results in input order.** `run()` returns one `OrchestratedResult` per input task, in input order (skipped tasks included).

## Config (new)

- `SWARM_MAX_CONCURRENT=8` — max simultaneous workers (0 = unlimited).
- `SWARM_MAX_COST_USD=0` — global cost cap for one orchestration (0 = unlimited).
- `SWARM_MAX_WALL_S=900` — wall-clock cap for one orchestration (0 = unlimited).

## Files

- **New:** `backend/app/services/swarm/pool.py`, `backend/tests/test_swarm_pool.py` (fake runners — no LLM/DB).
- **Modified:** `backend/app/services/swarm/orchestrator.py` (extract `run_task_with_retries`; keep `orchestrate` behavior), `backend/app/services/tool_handlers/swarm_tools.py` (opt into pool), `backend/app/config.py`, `.env.example`, roadmap doc.

## Acceptance

1. 12 subtasks with `max_concurrent=4` → `max_active == 4`, all 12 complete, results in input order.
2. Cost cap (`max_concurrent=1`, `cost_fn` = 1.0/task, budget 2.5) → 3 complete, 9 skipped with a budget error.
3. Wall-time cap (runner sleeps, tiny budget) → tasks after the first wave skipped.
4. A failing task retries (attempts=2) then escalates to `general-purpose` on the final retry.
5. Progress callback records per-worker events including `pool_started` / `pool_finished`.
