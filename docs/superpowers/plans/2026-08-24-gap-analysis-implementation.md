# Gap Analysis Implementation Plan (2026-08-24)

> Implementation: REQUIRED SUB-SKILL: superpowers:subagent-driven-development (chosen by user). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement ALL confirmed open gap-analysis items (P0/P1/P2) in one pass, fixing verified open issues in the "Zhanlu" platform agent chain, plus rewrite the working memory (MEMORY.md). Already-implemented items (P0-1 reasoning passback, P1-6 token refresh single-flight, P1-7 schema-slice denylist, P2-13 message feedback, file-upload chip fix) are regression-only — do NOT re-implement.

**Architecture:**
- **P0-2 parallel tool calls**: LLM payload declares `parallel_tool_calls` capability, gated by model capability (DeepSeek/OpenAI-compatible on, Anthropic excluded), letting the v3 loop consume multiple tool_calls in one turn.
- **P0-3 Agent Harness wiring**: Wrap the main chat loop (v3 streaming FSM) with AgentRun/AgentRunStep recording, gated by `AGENT_HARNESS_ENABLED` (default False). Provide run-status query + resume endpoints. Execution path unchanged.
- **P1-4 MCP client**: Implement stdio forwarding (initialize → tools/list → tools/call) using the official MCP Python SDK (already in requirements), replacing `_mcp_proxy_handler` stub.
- **P1-5 Context economics**: Per-model tool output caps (e.g. 128k model = 16000 chars), smart tool-result truncation (head/tail preserved + middle summary), three-tier compaction ladder (raw → microcompact → full compact).
- **P1-8 Offline eval completion**: Verify `eval_harness.py` completeness, add LLM-as-judge scoring + CI wiring.
- **P2-9 Durable global memory**: Postgres-backed learning graph (new SQLAlchemy model + Alembic migration), reusing `memory_advanced/vector_memory_store.py` semantic retrieval, gated by `LEARNING_GRAPH_DB_ENABLED` (default False), JSON fallback when DB unavailable.
- **P2-10 Final-answer quality floor**: Deterministic quality check (non-empty, non-apology bounce-back, substantive content) + 1 forced synthesis retry, verify `_data_rows_fallback` hides F* columns, no infinite loops.
- **P2-11 Realtime voice mode (phased P2-11a)**: WebSocket voice channel + existing ASR/TTS tools streamed + chat UI voice-mode toggle, gated by `REALTIME_VOICE_ENABLED` (default False).
- **P2-12 Decompose agents.py**: Pure refactor extracting the shared tool-execution core into `services/agent_loop/`, no behavior change, acceptance = existing 750+ tests all green.

## Tech Stack

- Backend: Python 3.11 + FastAPI + SQLAlchemy + Alembic + asyncpg + SSE/WebSocket; LLM layer `llm_service.py` + `llm_router.py` (models: deepseek-chat main chat, deepseek-v4-flash some subagents); MCP official SDK (`mcp>=1.0` already in requirements.txt).
- Frontend: React + Vite + Tailwind (`Chat.jsx` SSE event stream, `VoiceInput.jsx`, `mobile/MobileChatPage.jsx`).
- Tests: pytest (backend ~750+, container `python -m pytest -p no:jaxtyping -p no:cacheprovider`), Vitest (frontend); CI via `eval_pipeline.py`.

## Global Constraints

- ALL feature flags default False. Gate with `getattr(settings, "FLAG", False)`, NOT `os.environ.get()`.
- Mobile `mobile/MobileChatPage.jsx` must NOT regress; i18n via `frontend/src/lib/translations.js`.
- Dependency policy: NO new pip packages (MCP SDK already present).
- CJK regex: never `\b` around CJK; use lookarounds or wrap ASCII alternatives in `\b`.
- Limited RAM: no parallel pytest. Container testing from `/app` cwd (bind-mount `./backend:/app`): `/usr/local/bin/python -m pytest -p no:jaxtyping -p no:cacheprovider`. Local: `cd backend && venv/bin/python -m pytest <file>`.
- Build gotcha: `npm run build` BLOCKED by safe-delete shim. Use `./node_modules/.bin/vite build --emptyOutDir=false` + `docker restart zhanlu-frontend`. Backend redeploy = `docker restart zhanlu-backend`.
- `app.services.config` does NOT exist — always `from app.config import settings`.
- Router `ImportError` is silent: verify with `docker exec zhanlu-backend python -c "from app.routers.<n> import router"`. Restart backend after router edits.

## Overall Strategy

Dependency order: pure refactor first (P2-12 extraction, zero behavior change, test-green safety net), then behavior enhancements on clean modules (P0-2/P0-3/P2-10); P1 items and remaining P2 are mutually independent (can run in parallel); P2-11 uses design-first (brainstorming) and ships P2-11a in phases. Every task: write failing test (TDD) first, then implement, with executable acceptance criteria.

---

## Task 1: Working memory rewrite (prerequisite, zero code risk)

**Files:**
- `[MODIFY] .codebuddy/memory/MEMORY.md`

- [ ] **Step 1**: Read the full `/home/ysk2025/zhanlu_7_30/.codebuddy/memory/MEMORY.md` (currently over size limit, truncated on injection).
- [ ] **Step 2**: Consolidate and deduplicate: merge related entries, remove outdated/redundant info, mark implemented items as DONE, keep open gaps as OPEN with flags.
- [ ] **Step 3**: Rewrite in place as a structured memory (project conventions / implemented / open items / command cheat-sheet) under the size limit.
- [ ] **Step 4**: Verify the file injects fully without truncation.

**Acceptance:** File fully injectable and under size limit; no implemented item marked as todo; all valid conventions retained.

---

## Task 2: P2-12 Decompose agents.py (pure refactor, XL)

**Files:**
- `[NEW] backend/app/services/agent_loop/__init__.py`: export public API (`execute_tool_batch`, `apply_guardrails`, `enforce_tool_caps`, `maybe_force_finish_line`, `maybe_wrap_up_nudge`, `pause_for_approval`, SSE event builders, fallback message builders).
- `[NEW] .../agent_loop/tool_executor.py`: tool-batch execution core extracted from the three sites — v2 loop (~3469), resume path (~4791), `_stream_llm_with_tools` (~7328) — with asyncio.gather parallel execution, per-tool output caps, exception isolation, `_run_tool_batch` (~11223) logic.
- `[NEW] .../agent_loop/guardrails.py`: batch-level guards above `tool_loop_guardrails.py` (guard effectiveness, steering, finish-line forcing, wrap-up nudge).
- `[NEW] .../agent_loop/sse_builders.py`: SSE event builders `tool_progress`/`tool_calls`/`quality_eval`/`content_replace` (aligned with frontend `Chat.jsx` consumption format).
- `[NEW] .../agent_loop/fallbacks.py`: `_GENERIC_EMPTY_CONTENT_FALLBACK` (~7400) and fallback message builders.
- `[MODIFY] backend/app/routers/agents.py`: the three loops call the `agent_loop` public API; rest of code untouched.

**Acceptance:** All existing backend tests (750+) pass unchanged, zero modified test lines; `git diff` shows no behavior difference (test baseline + golden logs for key paths); new modules have own unit tests (tool_executor parallel/exception, guardrails trigger conditions, sse builder format, fallbacks copy).
**Verify:** container `docker exec zhanlu-backend python -m pytest tests/ -x -q -p no:jaxtyping -p no:cacheprovider` (sharded, no parallel); router import `docker exec zhanlu-backend python -c "from app.routers.agents import router"`.

---

## Task 3: P0-2 parallel_tool_calls dispatch

**Files:**
- `[MODIFY] backend/app/services/llm_service.py`: new `_supports_parallel_tool_calls(model)` (deepseek*/openai prefix match, anthropic excluded) + config flag `LLM_PARALLEL_TOOL_CALLS_ENABLED` (default False, in config.py); conditionally inject `"parallel_tool_calls": True` at payload construction (~42).
- `[MODIFY] backend/app/routers/agents.py`: 5 payload sites (5362 v2, 6329 guardrail/retry, 6857 `_call_llm_with_tools`, 7390 `_stream_llm_with_tools` base_payload, 11986 automation) all route through the helper (after P2-12, sites may converge into agent_loop or stay in agents.py — follow actual refactor result).
- `[MODIFY] backend/app/services/sub_agent_reliability.py`: payload injection in `call_llm_with_reliability` (reuse existing temperature-omission location).
- `[MODIFY] backend/app/services/tool_handlers/delegation_tools.py` + `edia_delegation_tools.py`: payload injection in `_call_llm`.
- `[MODIFY] backend/app/config.py`: add `LLM_PARALLEL_TOOL_CALLS_ENABLED: bool = False` (LLM section).

**Acceptance:** unit tests assert deepseek/openai model payloads contain `parallel_tool_calls: True`, anthropic does not, flag-off does not; v3 loop existing `raw_tool_calls` iteration capability (gather points 4968/5065/5913/6305/11056/11241) integration test consumes multiple tool_calls in one turn.
**Verify:** new test_parallel_tool_calls.py + existing LLM tests all green.

---

## Task 4: P0-3 Harness wiring (recording layer, no execution change)

**Files:**
- `[MODIFY] backend/app/services/harness/run_service.py`: confirm/complete `create_run`, `append_step`, `finalize_run`, `get_run_status`, `list_steps` (aligned with existing AgentRun/AgentRunStep models).
- `[NEW] backend/app/routers/agent_runs.py`: GET `/runs/{run_id}` (status + steps), POST `/runs/{run_id}/resume` (replay from last persisted step), GET `/conversations/{conv_id}/runs`; reuse existing auth deps.
- `[MODIFY] backend/app/routers/agents.py`: wrap v3 streaming loop (`_stream_llm_with_tools`) with `AGENT_HARNESS_ENABLED` (config.py, default False) gated recording — start run on first tool call, append step per tool batch (incl. checkpoint_sink.py), finalize at turn end; execution path unchanged.
- `[MODIFY] backend/app/services/harness/checkpoint_sink.py`: add fields as needed (tool batch input/output summaries, token counts).
- `[MODIFY] backend/app/main.py`: register `agent_runs.py` router (verify import — silent ImportError risk).

**Acceptance:** flag-off zero impact (full test suite passes); flag-on normal chat generates AgentRun/AgentRunStep rows; resume endpoint replays last step; unit tests (mocked loop + fake DB).
**Verify:** `docker exec zhanlu-backend python -c "from app.routers.agent_runs import router"`; integration test asserts row counts in test DB.

---

## Task 5: P1-4 MCP SDK client (dependency already satisfied)

**Files:**
- `[MODIFY] backend/app/services/tool_handlers/mcp_proxy.py`: rewrite with `mcp` SDK `stdio_client` + `ClientSession` — `probe_mcp_server` becomes async probe (initialize → tools/list), `_mcp_proxy_handler` implements tools/call forwarding, request cancellation, connection pooling (`_mcp_clients` reuse), logging (no secrets/PII), timeout (keep 10s probe timeout).
- `[MODIFY] backend/app/services/tool_handlers/__init__.py`: export new signatures as needed.
- `[NEW] backend/tests/test_mcp_proxy.py`: stub stdio MCP server (in-memory JSON-RPC), register tool, assert round-trip success + error path through `_mcp_proxy_handler`.

**Acceptance:** stub server round-trip test passes; SDK client + session close cleanly (no leak); failure returns `{"success": False, "error": ...}` structure unchanged (backward compatible with existing callers).
**Verify:** `docker exec zhanlu-backend python -m pytest tests/test_mcp_proxy.py -p no:jaxtyping -p no:cacheprovider`.

---

## Task 6: P1-5 Context economics

**Files:**
- `[MODIFY] backend/app/config.py`: add `TOOL_OUTPUT_CAP_BY_MODEL` (dict, e.g. `{"deepseek-chat": 16000, ...}`, default fallback 8000) + `CONTEXT_ESCALATION_LADDER` (e.g. `{"raw_ratio": 0.9, "microcompact_tokens": 30000, "full_compact_tokens": 50000}`), keep `TOOL_MAX_OUTPUT_CHARS=8000` as lower-bound compat.
- `[MODIFY] backend/app/services/artifacts/data_profiler.py` (entity master filter caller) or `[NEW] services/tool_output/smart_truncate.py`: head/tail preservation + middle summary (reuse `summarize_tool_result` existing row_count/total semantics).
- `[MODIFY] backend/app/services/compaction/pre_api_prune.py`: ladder: raw history ≤ 90% window passes through → over-limit first `microcompact.py` → still over-limit `full_compact.py` (replaces original 0.8-threshold binary trigger, keep `COMPACTION_AUTO_THRESHOLD` compat).
- `[MODIFY] backend/app/services/compaction/token_estimator.py`: fix estimates as needed (verify current implementation).

**Acceptance:** unit tests — smart truncation head/tail/middle summary correct; per-model cap selection correct; ladder triggers in order without double compaction; existing compaction tests no regression.
**Verify:** relevant unit tests + `pytest tests/ -k compaction`.

---

## Task 7: P1-8 Offline eval completion

**Files:**
- `[MODIFY] backend/app/services/synexia/eval_harness.py`: verify `EvalRunner` completeness (golden scenarios loading, metric aggregation); add LLM-as-judge scorer (reuse `llm_service` json_schema capability, output rubric score + reason, fall back to deterministic metrics on failure).
- `[MODIFY] backend/app/services/eval_pipeline.py`: wire CI entry (`python -m app.services.eval_pipeline --ci`, non-zero exit on failure).
- `[MODIFY] backend/tests/eval/golden_scenarios.json`: add scenarios as needed.
- `[MODIFY] existing tests/test_eval_harness.py, test_eval_ci.py, test_eval_pipeline.py`: add LLM-as-judge scoring + CI exit-code assertions.

**Acceptance:** offline run through golden scenarios; judge scoring output structure stable; CI mode non-zero exit above threshold.
**Verify:** container eval_pipeline --ci (dark mock or real small model to avoid resource limits).

---

## Task 8: P2-9 Durable global memory

**Files:**
- `[NEW] backend/app/models/learning_memory.py`: `LearningGraphEntry` (user_id, agent_key, content, embedding vector, metadata, created_at) + Alembic migration (position per actual repo `alembic/versions`).
- `[MODIFY] backend/app/services/learning_graph.py`: DB read/write (`LEARNING_GRAPH_DB_ENABLED` gate, default False), reuse `memory_advanced/vector_memory_store.py` semantic retrieval; JSON-file fallback when DB unavailable.
- `[MODIFY] backend/app/services/memory_consolidation.py` (or new consolidation job): session-insight distillation task aggregating fragmented memories into higher-level entries.
- `[MODIFY] backend/app/config.py`: add `LEARNING_GRAPH_DB_ENABLED: bool = False`.
- `[NEW] backend/tests/test_learning_memory_db.py`: migration applies; DB read/write; consolidation; fallback when DB off.

**Acceptance:** migration applies on test DB; flag-off full test suite no regression; fallback path unit-tested.
**Verify:** `alembic upgrade head` (test DB) + pytest new tests.

---

## Task 9: P2-10 Final-answer quality floor

**Files:**
- `[MODIFY] backend/app/routers/agents.py` (or extracted agent_loop/finalize.py): deterministic quality check — non-empty + non-apology/bounce-back copy + substantive content (beyond `_data_rows_fallback` fallback); failure triggers 1 forced synthesis retry (with "give substantive conclusion" instruction, no repeated tool calls); only then emit fallback.
- `[MODIFY] backend/app/services/answer_verification.py`: verify existing 4 call sites wired into finalize gate (add if missing).
- `[MODIFY] backend/app/routers/agents.py`: verify `_data_rows_fallback` renders deterministic markdown tables and hides F*/empty columns (was in an approved P2 fix — regression verify).

**Acceptance:** unit tests — degenerate answer triggers forced retry exactly once; retry produces substantive answer; fallback table hides F* columns; no infinite loop (cap 1).
**Verify:** new `tests/test_final_answer_quality.py` + existing answer-related tests all green.

---

## Task 10: P2-11 realtime voice (P2-11a phased)

**Files:**
- `[NEW] backend/app/routers/realtime_ws.py`: WebSocket `/ws/realtime/voice`, session state machine (idle/listening/transcribing/responding), reuse existing `transcription_tool`/`tts_tool` (audio frame streaming both ways); `REALTIME_VOICE_ENABLED` gate (default False).
- `[MODIFY] backend/app/main.py`: register WS router (verify import not silently failing).
- `[MODIFY] frontend/src/components/chat/VoiceInput.jsx`: add voice-mode toggle (connection state, waveform animation, tap-to-talk, streaming transcription display); Web Speech API kept as degradation path.
- `[MODIFY] frontend/src/pages/Chat.jsx`: connect WS voice channel (SSE event stream unchanged; voice mode parallel); mobile `mobile/MobileChatPage.jsx` sync toggle without regression.
- `[MODIFY] frontend/src/lib/translations.js`: add voice-mode copy keys.
- `[NEW] backend/tests/test_realtime_ws.py`: handshake, audio→transcription→agent turn→TTS frame return (mock ASR/TTS), state-machine illegal migration rejected.

**Acceptance:** WS handshake + full-chain mock round-trip pass; UI toggle usable; latency budget test (first TTS frame < 1.5s, mock env); flag-off: WS rejects connection, frontend hides toggle.
**Verify:** pytest new tests + frontend `./node_modules/.bin/vite build --emptyOutDir=false` (NOT `npm run build` — safe-delete shim blocks).

**Design note (UI increment only):** voice mode is an overlay on the existing dark chat UI: voice bar above the composer (mic main button + waveform animation + connection status dot + live transcription text), tap-to-talk hold-to-speak, release-to-send, swipe-up-to-cancel; transcription and agent streaming replies share the existing SSE rendering pipeline. Mobile keeps isomorphic gesture (long-press to speak), does not break existing composer layout. Visual: existing theme colors + rounded cards + micro-animations (pulse recording indicator, waveform bars) + hover feedback.

---

## Task 11: Final system-wide verification

- [ ] **Step 1**: Full backend test suite (sharded, no parallel) all green; frontend Vitest all green + `vite build` passes.
- [ ] **Step 2**: `docker exec zhanlu-backend python -c` imports every new/modified router (agents, agent_runs, realtime_ws).
- [ ] **Step 3**: `alembic upgrade head` succeeds (learning memory migration).
- [ ] **Step 4**: Manual end-to-end: chat → tool calls (parallel multi-tool) → final-answer quality gate → voice-mode toggle → run-status query.
- [ ] **Step 5**: Feature-flag default audit: all new flags False; behavior with flag-on isolated from flag-off.
- [ ] **Step 6**: [skill:requesting-code-review] code review + [skill:finishing-a-development-branch] wrap-up.

---

## Self-Review Checklist

- [x] Spec coverage: all 11 tasks cover every open item (P0-2, P0-3, P1-4, P1-5, P1-8, P2-9, P2-10, P2-11a, P2-12) + memory rewrite + final verification.
- [x] No placeholders: every task has file-level and function-level modification lists, per-task acceptance criteria, test coverage, and final verification.
- [x] Type consistency: `execute_tool_batch` signature consistent across the 3 loop sites; fallback functions match current agents.py signatures.
- [x] All flags default False: `LLM_PARALLEL_TOOL_CALLS_ENABLED`, `AGENT_HARNESS_ENABLED` (existing), `LEARNING_GRAPH_DB_ENABLED`, `REALTIME_VOICE_ENABLED`.
- [x] 750+ existing tests must pass: every task includes full-suite verification.
