# IBM Agent-Orchestration Hardening — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Close the six IBM agent-orchestration gaps in the Zhanlu backend: shared context bus, provider fault tolerance, sub-agent pipeline activation, tool-selection efficiency + measurement, telemetry→routing feedback, and ReAct-loop de-duplication.

**Architecture:** New services under `app/services/` (`context_bus.py`, `routing_feedback.py`), surgical edits to `llm_service.py` / `config.py` / `delegation_tools.py` / `agents.py`, and a phased extraction of the ReAct loop into `app/services/agent_loop/react_loop.py` behind a flag. Every task is TDD, run inside `docker exec zhanlu-copy-backend python -m pytest ... -q`.

**Tech Stack:** FastAPI, httpx, SQLAlchemy, pytest (+pytest-asyncio), existing MiniLM embeddings (`dynamic_tools.py`), existing `provider_health.py` circuit breaker.

**Verified ground truth (2026-10-08 exploration):**
- Circuit breaker EXISTS: `app/services/provider_health.py` (threshold 3, cooldown 30s), consumed by `call_llm` (llm_service.py:646,666,704) and `stream_chat_completion` (1010,1032,1047).
- Ordered provider fallback EXISTS via `get_llm_providers()` (llm_service.py:305-346) reading `LLM_FALLBACK_PROVIDERS` (config.py:204) — **but it is empty**, so effectively single-provider.
- Timeouts hard-coded: 120s (llm_service.py:652, 1019), 30s (841, 785). No `LLM_TIMEOUT` setting.
- NO same-provider backoff retry (only provider-to-provider failover + one bounded retry in v3 loop agents.py:13932-13938). No tenacity.
- Context: thin shared `_v3_conv_ctx` (agents.py:11664) goes to both paths, but FSM uses `ContextManifest` (synexia/context_assembler.py:92-117) while ReAct uses raw `llm_messages` + compaction (`services/compaction/`). FSM never sees compaction; ReAct never sees the manifest.
- `SYNEXIA_FSM_ENABLED` default **True** (config.py:590).
- Swarm runtime complete and tested: `SwarmRuntime.run()` (swarm/runtime.py:94), `SwarmCoordinator.run_task/spawn_agent` (swarm/team_registry.py:200/226), tool whitelist enforcement (runtime.py:199-201). Only reachable via `swarm_*` tools; `AGENT_HARNESS_ENABLED=False` (config.py:1522).
- Tool selection: `select_tools_for_turn` (services/dynamic_tools.py:163-204), `TOOL_LOADING_MODE="dynamic"` default (config.py:518), core + top-12 periphery by MiniLM cosine, fail-open. **No miss-rate measurement exists.**
- Telemetry: `agent_invocations` row per turn (`services/agent_invocations.py:47-120`, written agents.py:19006-19046) has duration/tokens/cost/tool_call_count but **no route, no outcome, and nothing reads it back for routing**. TokenTracker not fed from main loop.
- Duplication: 3 hand-rolled ReAct loops — v2 `add_message` (agents.py:6426-8448), `/resume` (8451-9370), v3 `event_stream` (12840-19083). Guardrail plumbing copy-pasted 3× (:7344, :8958, :14554). v2 has ZERO frontend callers (only tests/scripts).
- **Test command:** `docker exec zhanlu-copy-backend python -m pytest tests/<file> -p no:jaxtyping -p no:cacheprovider -q`
- **Restart:** `docker restart zhanlu-copy-backend zhanlu-copy-sandbox-worker`

---

## Workstream A — Fault tolerance (failover + self-healing) — SMALL, DO FIRST

### Task A1: Configurable LLM timeouts

**Files:**
- Modify: `backend/app/config.py` (near line 204, after `LLM_FALLBACK_PROVIDERS`)
- Modify: `backend/app/services/llm_service.py` (lines 652, 1019, 841, 785)
- Test: `backend/tests/test_llm_resilience.py` (new)

**Step 1: failing test**
```python
# backend/tests/test_llm_resilience.py
import importlib
import pytest

def test_timeout_settings_exist():
    from app.config import settings
    assert settings.LLM_TIMEOUT_S == 120.0
    assert settings.LLM_SYNC_TIMEOUT_S == 30.0

def test_call_llm_uses_configured_timeout(monkeypatch):
    from app.services import llm_service
    captured = {}
    class FakeClient:
        def __init__(self, timeout=None, **kw): captured["timeout"] = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): raise AssertionError("not reached")
    monkeypatch.setattr(llm_service.httpx, "AsyncClient", FakeClient)
    # provider list with one provider; call fails before post via empty url -> just assert timeout captured
    ...
```
(Full test file written during implementation; the assertion target is that `httpx.AsyncClient(timeout=...)` receives `settings.LLM_TIMEOUT_S`.)

**Step 2:** run — expect FAIL (no attribute `LLM_TIMEOUT_S`).
**Step 3:** add to config:
```python
LLM_TIMEOUT_S: float = 120.0      # per-call async chat/stream timeout
LLM_SYNC_TIMEOUT_S: float = 30.0  # sync JSON / embedding calls
```
Replace hard-coded values: llm_service.py:652 `timeout=settings.LLM_TIMEOUT_S`, :1019 same, :841 and :785 `settings.LLM_SYNC_TIMEOUT_S`.
**Step 4:** run — PASS. **Step 5:** commit `feat(llm): configurable per-call timeouts`.

### Task A2: Same-provider transient backoff retry

**Files:**
- Modify: `backend/app/services/llm_service.py` (inside `call_llm` provider loop at :645, and `stream_chat_completion` at :977)
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_llm_resilience.py`

**Design:** Before `continue`-ing to the next provider on a transient error, retry the SAME provider up to `LLM_PROVIDER_RETRIES` (default 2) with linear backoff `LLM_RETRY_BACKOFF_BASE_S * attempt` (default 1.0s → 1s, 2s). Only for `is_transient` errors (429/5xx/timeout). Non-transient (4xx auth/schema) → fail immediately as today. Add helper:
```python
async def _attempt_with_retry(fn, *, retries: int, base_s: float, on_retry):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except Exception as e:
            if not is_transient(e) or attempt >= retries:
                raise
            last_exc = e
            delay = base_s * (attempt + 1)
            logger.warning("llm transient error (attempt %d/%d): %s — retrying in %.1fs",
                           attempt + 1, retries + 1, e, delay)
            on_retry(e, attempt)
            await asyncio.sleep(delay)
    raise last_exc
```
Config: `LLM_PROVIDER_RETRIES: int = 2`, `LLM_RETRY_BACKOFF_BASE_S: float = 1.0`.

TDD: test with a stub provider fn raising 429 twice then succeeding → succeeds, sleeps recorded (monkeypatch `asyncio.sleep`); 401 → raises immediately, no retry.

### Task A3: Populate fallback providers (same tier pinning)

**Files:**
- Modify: `backend/.env` — set `LLM_FALLBACK_PROVIDERS` with a real second OpenAI-compatible endpoint (same model tier as `LLM_MODEL`; ask user for the key or reuse existing DEEPSEEK key if present in env).
- Modify: `backend/app/services/llm_service.py::get_llm_providers` — support optional `"tier"` field per fallback entry; when `task_type` routing resolves a model, filter fallbacks to entries whose `tier` matches the primary's tier (default `"default"`); if the filtered list is empty, fall back to the unfiltered list (fail-open, never zero providers).
- Modify: `backend/app/main.py` startup — one-shot warning log if `LLM_FALLBACK_PROVIDERS` empty: "single point of failure: no LLM fallback providers configured".
- Test: `test_llm_resilience.py::test_fallback_tier_filtering` — primary tier `"reasoning"`, fallbacks one `"reasoning"` + one `"fast"` → only same-tier fallback used; both-fast-only → all used.

### Task A4: Mid-stream failover is connect-time-only — document + guard

`stream_chat_completion` (llm_service.py:977-983) already retries before first chunk but cannot switch mid-stream. Add a docstring note + emit `record_failure` on mid-stream exception (already does at :1047 — verify with a unit test asserting breaker sees the failure). No behavior change.

---

## Workstream B — Shared context bus (fix the "two brains") — MEDIUM

### Task B1: ContextBus service (new module)

**Files:**
- Create: `backend/app/services/context_bus.py`
- Test: `backend/tests/test_context_bus.py` (new)

**Design:** per-conversation singleton store. Sections: `system` (agent config, tools, skills), `transcript` (reference to the live `llm_messages` list), `artifacts`, `tool_digest` (rolling digests of tool results this turn), `task_state` (plan/gate/verify records), `memory` (consolidated turn memories). Retrieval: embedding cosine over section texts using the same MiniLM model loader as `dynamic_tools.py` (import its model accessor; if unavailable, keyword scoring fallback).

```python
@dataclass
class ContextSection:
    key: str; text: str; meta: dict; updated_at: float

class ContextBus:
    def __init__(self, conversation_id: str): ...
    def publish(self, key: str, text: str, meta: dict | None = None): ...
    def get(self, key) -> ContextSection | None: ...
    def retrieve(self, query: str, k: int = 5) -> list[ContextSection]: ...  # cosine, fallback lexical
    def to_fsm_manifest(self) -> dict:   # shaped like ContextManifest: items/token_estimate/...
    def format_for_react(self) -> str:   # compact block for system prompt injection
    def digest_tool_result(self, tool_name: str, result: dict, max_chars: int = 1200): ...

_BUSSES: dict[str, ContextBus] = {}
def get_context_bus(conversation_id: str) -> ContextBus: ...  # create-or-get
def drop_context_bus(conversation_id: str): ...               # end-of-turn cleanup
```

TDD tests: publish/get roundtrip; retrieve ranks relevant section first (semantic) and works when embeddings unavailable (lexical fallback); `to_fsm_manifest` produces the keys `context_assembler.ContextManifest` consumers expect (`items`, `token_estimate`, `available_tools`, `available_skills`, `user_attachments`); `format_for_react` truncates per-section and includes section headers; singleton reuse + drop.

### Task B2: Wire the bus into the v3 branch point (both paths, one object)

**Files:**
- Modify: `backend/app/routers/agents.py` around :11664 (where `_v3_conv_ctx` is built) and the FSM run block :11965-12053, and the ReAct `event_stream` :12840+ loop.
- Test: `backend/tests/test_context_bus_wiring.py` (new, integration-style with fakes)

**Behavior (keep FSM skeleton untouched — this is context plumbing only):**
1. After `_v3_conv_ctx` is built: `_bus = get_context_bus(conversation_id)`; publish `system` (agent name, resolved tools list names, skills), `memory` (from `_v3_conv_ctx` transcript/artifacts), `task_state` if resuming.
2. FSM path: pass `ExecutionRequest(..., context_manifest=None)` as today, but ALSO attach the bus to the request via a new optional field `context_bus` on `ExecutionRequest` (fsm.py:127 region) — FSM's CONTEXT stage (`_run_context`, fsm.py:424-443) merges bus sections into `context_manifest` instead of assembling from scratch when a bus is present (`assemble_context` stays the fallback). FSM OBSERVE/VERIFY publish digests back into the bus.
3. ReAct path: system prompt injection at :12374-12376 becomes `format_followup_context_block(_v3_conv_ctx) + "\n" + _bus.format_for_react()` (dedupe sections already covered by the followup block: skip `transcript`/`artifacts` keys already present). Per-iteration: after each tool batch, `_bus.digest_tool_result(...)` for successful substantive results (reuse the same decision point that feeds `_condense_data_agent_results`, agents.py:13917).
4. End of turn (both terminal paths, near agents.py:18885 and :12247 region): `drop_context_bus(conversation_id)`.

TDD: integration test — fake FSM + fake loop both receive the SAME bus object; bus sections published in ReAct visible to `to_fsm_manifest()`; tool digest appears in ReAct prompt on next iteration; bus dropped after turn.

### Task B3: Compaction visibility for FSM (bus as the bridge)

**Files:**
- Modify: `backend/app/services/synexia/fsm.py` (`_run_context`)
- Test: extend `test_context_bus_wiring.py`

The FSM never learns about compaction. When the ReAct path compacts (reactive `auto_compact_if_needed`, agents.py:14213), publish a `compaction` section on the bus (`"N older messages compacted; summary: ..."`, reuse the compaction summary text). FSM `_run_context` includes `bus.get("compaction")` as a manifest item when present. If FSM runs first and FSM verifies/observes produce plan/gate records, publish them so a same-conversation ReAct fallback (agents.py:12054-12059 fallthrough) sees prior plan state via `format_for_react()`.

---

## Workstream C — Agent selection: swarm sub-agent for the data pipeline — MEDIUM

### Task C1: `data-analyst` agent definition + flag

**Files:**
- Modify: `backend/app/services/swarm/agent_definitions.py` — add `data-analyst` definition: own system prompt (data-grounding rules mirroring the harness DATA GROUNDING rule), `tools` whitelist = exactly `_DATA_AGENT_TOOLS` from `delegation_tools.py` (import or duplicate the list with a comment tying them), `max_iterations` default 6.
- Modify: `backend/app/config.py` — `DATA_AGENT_SWARM_ENABLED: bool = False`.
- Test: `backend/tests/test_swarm_data_pipeline.py` (new) — definition exists, whitelist matches `_DATA_AGENT_TOOLS`, flag default False.

### Task C2: Swarm-backed `ask_data_agent` path

**Files:**
- Modify: `backend/app/services/tool_handlers/delegation_tools.py` (`_ask_data_agent`, :188-193; insert after the planner/clarify short-circuits ~:293, before the hand-rolled sub-loop at :345)

**Behavior:** when `settings.DATA_AGENT_SWARM_ENABLED` and no fastpath/planner answer: build `llm_fn` (wrap `call_llm` with endpoint passthrough, matching `SwarmCoordinator._llm` shape) and `tool_dispatcher` (wrap `agent_tools.execute_tool`, injecting `data_source_id` default = preferred/bound KB into args — this fixes the classic "internal tool requires data_source_id" failure mode at the dispatcher seam). Call `get_swarm_coordinator().run_task(...)` with the same question + progress emitter bridged to `context["progress_emitter"]`. Map `SwarmAgentResult.final_response`/`tool_calls` into the existing return shape (`last_rows`, `last_sql`, `final_text`) so downstream synthesis is unchanged. On any swarm exception → log + fall through to the legacy sub-loop (fail-open, exactly like the FSM fallthrough pattern).

Keep the existing delegation summary log; add `"mode=swarm"` vs `"mode=loop"` to it — that is the A/B measurement hook.

TDD: flag off → legacy loop used (existing tests unchanged); flag on → coordinator called with whitelisted dispatcher; dispatcher injects missing `data_source_id`; swarm exception → legacy loop result returned; summary log contains mode.

### Task C3: Latency/quality comparison readout

Reuse the summary logs: add a tiny aggregator in `app/services/agent_metrics.py` (`record_delegation(mode, wall_s, success, rows)`, exposed in `get_snapshot()` as `delegation_modes`). After enabling the flag in a dev environment, compare snapshots across representative questions. (No routing decision automation yet — YAGNI until data exists.)

---

## Workstream D — Tool selection efficiency + measurement — SMALL

### Task D1: Selection miss-rate telemetry

`select_tools_for_turn` already does lightweight retrieval (dynamic_tools.py:163-204). What's missing is knowing whether selection hurts.

**Files:**
- Modify: `backend/app/services/dynamic_tools.py` — `select_tools_for_turn(schemas, user_message, *, conversation_id=None, turn_key=None)` gains an optional recorder: when the loop later executes a tool whose name was NOT in the selected periphery set (and not in core), call `record_selection_miss(conversation_id, tool_name, selected_count)`.
- Modify: `backend/app/services/agent_metrics.py` — counters `tools_selected_total`, `tools_selection_miss`; expose in `get_snapshot()`.
- Test: `backend/tests/test_tool_selection_metrics.py` (new) — miss recorded only for out-of-set tools; fail-open when embeddings unavailable still counts selection; counters appear in snapshot.

Wire the miss callback at the tool-execution site in the shared loop (Workstream F makes this one place; until then, add at the v3 site agents.py tool-batch execution + v2 site).

### Task D2: Dynamic-mode audit test (guards the efficiency win)

Test with ~130 fake schemas that: dynamic mode returns core + ≤12 periphery (bounds), relevant tool by description similarity is included, and `TOOL_LOADING_MODE="all"` returns everything. Locks in the IBM "lightweight retrieval" behavior against regressions.

---

## Workstream E — Continuous optimization: telemetry → routing feedback — MEDIUM

### Task E1: Enrich `agent_invocations` with route + outcome

**Files:**
- Modify: `backend/app/models/agent_invocation.py` — add columns `route` (VARCHAR(32): `react|fsm|cache|workflow`), `outcome` (VARCHAR(32): `completed|failed|clarified|paused`), `tools_selected` (INTEGER), `delegation_mode` (VARCHAR(16), nullable).
- Modify: `backend/app/main.py::_specific_migrations()` — register the new columns (follow the existing `(column, DDL)` tuple pattern used for `q_sql_examples`).
- Modify: `backend/app/services/agent_invocations.py::record_invocation` — accept + persist the new fields (defaults, non-fatal).
- Modify: `backend/app/routers/agents.py` — set route at each terminal site (v3 legacy :19006 block, FSM done site ~:12247 region, cache-hit :11644, workflow :11539) and outcome from the existing completion status; pass `tools_selected` (len of selected list from D1 seam) and `delegation_mode` when available.
- Test: `backend/tests/test_invocation_enrichment.py` (new) — record_invocation persists route/outcome/tools_selected; unknown values coerced to None; router sites pass them (integration with fakes).

### Task E2: Routing feedback reader (closed loop)

**Files:**
- Create: `backend/app/services/routing_feedback.py`
- Modify: `backend/app/config.py` — `ROUTING_FEEDBACK_ENABLED: bool = False`
- Test: `backend/tests/test_routing_feedback.py` (new)

**Design (read-only advisor first — thresholds change only when the flag is on AND stats are significant):**
```python
@dataclass
class RouteStats:
    route: str; turns: int; success_rate: float; p50_ms: float; p95_ms: float; avg_cost: float

def compute_route_stats(db, *, window_hours=168, min_turns=20) -> list[RouteStats]: ...
def suggest_fsm_threshold(db) -> float | None:
    # returns adjusted classifier confidence threshold for routing to FSM:
    # if fsm success_rate >= react success_rate + 0.05 and fsm p95 <= react p95 -> lower threshold by 0.05
    # if fsm success_rate < react -> raise threshold by 0.05 (capped [0.3, 0.9]); None if insufficient data
def get_suggested_overrides(db) -> dict:  # consumed by the routing ladder + DEFAULT_TOOLS_BY_AGENT weighting later
```
Consumption: `_route_to_dedicated_system_agent` / FSM classifier site reads `get_suggested_overrides(db)` (cached 5 min in-process) only when `ROUTING_FEEDBACK_ENABLED`. Log-only mode when flag off (stats computable, suggestions logged at startup of each turn's route resolution — actually: log once per process, not per turn).

TDD: stats math on seeded rows; None when below min_turns; threshold adjust both directions + clamping; flag off → overrides not applied.

---

## Workstream F — De-duplicate the loop (v2 / resume / v3 → one generator) — LARGE, LAST, PHASED

**Strategy:** extract the ReAct loop into an async generator `run_react_loop(ctx: LoopContext)` in `app/services/agent_loop/react_loop.py`, then make all three endpoints consumers. Behind `REACT_LOOP_EXTRACTED: bool = False` so every phase is independently reversible. The v3 `event_stream` stays the reference implementation until F3 flips the flag; each phase keeps 100% event-sequence parity (asserted by a golden-event test).

### Task F1: LoopContext + extraction skeleton

**Files:**
- Create: `backend/app/services/agent_loop/react_loop.py`
- Create: `backend/app/services/agent_loop/loop_context.py` (dataclass: `db, conversation, messages, llm_messages, tools, endpoint, user_id, agent, max_iterations, resume_point: dict|None, emit: Callable, state: dict`)
- Test: `backend/tests/test_react_loop_extraction.py` (new)

Start by moving loop-local helpers that are currently duplicated: guardrail nudge plumbing (`_check_hallucination_guardrail` call sites :7344/:8958/:14554 → one `guardrail_nudge(...)` in react_loop), verify-on-stop, options-clarification hard-stop, tool-batch execution + activity emission. The generator yields typed event dicts (same shapes the v3 stream already yields). TDD: unit tests over a fake LLM fn — normal tool roundtrip, guardrail nudge path, clarification stop, resume_point continuation (llm_messages + pending tool injected).

### Task F2: v3 `event_stream` delegates to the generator (flag-gated)

**Files:**
- Modify: `backend/app/routers/agents.py` `event_stream` (:12840-19083): when `settings.REACT_LOOP_EXTRACTED`, the body becomes a thin adapter building `LoopContext` and `async for evt in run_react_loop(ctx): yield evt`; else legacy inline (unchanged).
- Test: golden parity test — run a scripted conversation (fake LLM with deterministic tool calls) through BOTH legacy and extracted paths; assert identical event sequence (types + key fields, ignoring timestamps). Flag default False keeps production untouched; flip in F5.

### Task F3: v2 becomes collect-then-return over the same generator

v2 has zero frontend callers but 4 backend-internal ones (tests/scripts). `add_message` (:6426-8448) becomes: build context → `events = [evt async for evt in run_react_loop(ctx)]` → fold events into `conv.to_dict()` shape (the existing tail logic :8448). Delete the inline v2 loop + its guardrail copy. v2 keeps its own FSM branch (already shared with v3).

Test: existing v2 tests (`tests/routers/test_workflow_chat_integration.py`, `test_smoke_e2e_contracts.py`) must pass unchanged.

### Task F4: `/resume` rides the generator via `resume_point`

`resume_conversation` (:8451-9370) restores `_resume_state` (:8559-8567) → pass as `LoopContext.resume_point` (llm_messages snapshot, iteration, pending tool). Delete the hand-rolled continuation loop and its guardrail/options copies. Frontend caller `SkillMessageBubble.jsx:87` unchanged (endpoint contract identical).

Test: resume with pending-tool-skip and pending-tool-execute scenarios via fakes; parity with old behavior on event fold-out (resume is non-stream JSON: collect-then-return like v2).

### Task F5: Flip the flag + delete dead code

Set `REACT_LOOP_EXTRACTED=True` in `.env`, run the full regression suite (`docker exec zhanlu-copy-backend python -m pytest tests -p no:jaxtyping -p no:cacheprovider -q`), restart containers, manual smoke of Chat + SkillAgent resume. Then delete the legacy inline loop bodies (keeping git history as the archive). Net: one loop, fixes land once.

---

## Execution order & risk

| Order | Workstream | Risk | Reversibility |
|---|---|---|---|
| 1 | A (fault tolerance) | Low | config-gated |
| 2 | B (context bus) | Medium | flag + fail-open fallbacks |
| 3 | D (tool metrics) | Low | additive |
| 4 | C (swarm pipeline) | Medium | flag default False, fail-open |
| 5 | E (telemetry feedback) | Low | flag default False, read-only |
| 6 | F (de-dup) | **High** | per-phase flag, golden parity tests |

Commit after every task. Never edit agents.py without reading the anchor region first (file is 19k lines).
