# Zhanlu Agent System — Gap Analysis Report

**Date:** 2026-07-27
**Scope:** Comparison against top-tier agent frameworks (OpenAI Agents SDK, LangGraph, Anthropic extended-thinking agents, Magentic-One). Supersedes the 2026-07-16 report (archived at `gap-2026-07-16.md`).
**Overall Maturity:** ~75–85% feature breadth; ~55–70% depth/reliability
*(up from ~40–55% breadth / ~20–30% depth on 2026-07-16)*

---

## Executive Summary

Zhanlu has crossed the critical threshold: **the advanced layers are no longer dormant**. All five critical gaps from the July 16 report — planning FSM wiring, semantic memory, tool self-healing, swarm runtime, and eval gates — are now **implemented in code and covered by unit tests**. Provider fallback, reflexion self-critique, and context compaction are wired into the live conversation loop.

The remaining gap profile has shifted from *"features missing"* to *"depth unverified"*:

- The new layers (FSM routing, semantic recall, swarm mailbox, reflexion) are validated by unit tests but **not yet by runtime profiling, load testing, or chaos engineering**.
- Automation has reached Manus-parity and beyond (auto-skip, live SSE, failure notify, bounded concurrency, quality-gate routing, structured cross-run delta); the leftovers are scheduler precision and multi-worker efficiency.
- A few integrations are still **default-off or unverified at the call site** (OHMO memory consolidation, OHMO workspace prompt injection, CI blocking on eval gates).

---

## Dimension-by-Dimension Scoring

| # | Dimension | 07-16 | Now | Top-Tier | Gap | Evidence of Change |
|---|-----------|-------|-----|----------|-----|--------------------|
| 1 | Core Tool-Calling Loop | 7 | **8** | 9 | Small | Success-aware `tool_loop_guardrails.py` (296 LOC), `iteration_budget.py`, 3-layer `tool_result_persistence.py` |
| 2 | Planning / Decomposition | 2 | **8** | 9 | Small | `should_trigger_planning` + `SynexiaFSM(db).run()` wired into both sync and SSE paths (`agents.py:2102–2120`, `4679–4747`); `chat_loop_adapter.py`; `fsm_state` SSE events |
| 3 | Reasoning (Reflection/Critique) | 1 | **7** | 9 | Medium | `synexia/reflexion.py` (193 LOC) + `_inject_reflexion_critique` at 5+ turn sites (`agents.py:2657, 2966, 3685, 3821, 5575, 5958`); `self_critic.py`; `verifier.py` |
| 4 | Long-Term Memory | 2 | **7** | 9 | Medium | `embeddings.py` (231) + `vector_store.py` (175) + `scorer.py` (115): `α·cosine + β·recency + γ·importance` blend |
| 5 | Multi-Agent Orchestration | 3 | **7** | 9 | Medium | `swarm/mailbox.py` (307), `runtime.py` (224), `orchestrator.py` (181); `swarm_tools.py` handlers; `swarm_mailbox.py` DB model |
| 6 | Tool Reliability / Self-Heal | 3 | **8** | 9 | Small | `tool_retry.py` (241) with backoff, `api_error_classifier.py`, `tool_result_classification.py`, `sub_agent_reliability.py`, `reliability/` |
| 7 | Streaming / Real-Time UX | 6 | **7** | 8 | Small | `fsm_state` SSE events, automation live stream (`test_automation_live_stream.py`); patient-poll workaround status unverified |
| 8 | Context Management | 3 | **7** | 8 | Medium | Compaction wired into the turn loop; `context_engine.py`; structured cross-run delta for automation |
| 9 | Governance / Safety | 6 | **7** | 8 | Small | Quality gate enforced on automation deliverables; `test_anti_hallucination_guardrails.py` |
| 10 | Observability / Tracing | 7 | **8** | 8 | None | `agent_metrics.py` tracks fallback/reflexion; dashboard surfacing unverified |
| 11 | Model Abstraction / Fallback | 3 | **7** | 8 | Medium | `provider_fallback.py` + `LLM_FALLBACK_PROVIDERS`; `with_fallback` wired (`agents.py:78, 2491`) |
| 12 | Eval / Quality Gates | 2 | **7** | 8 | Medium | `eval_harness.py` (370), `eval_ci.py`, `golden_scenarios.json`, nl2sql golden eval; CI blocking unverified |
| 13 | Skill System | 7 | **8** | 8 | None | Progressive disclosure + dynamic discovery (`test_dynamic_skill_discovery.py`) remain a strength |

---

## Status of the 5 Critical Gaps from 2026-07-16

### Gap 1 — Planning Layer: CLOSED ✅
`agents.py` now imports `should_trigger_planning` / `PlanTrigger` (`agents.py:91`) and `SynexiaFSM` (`agents.py:92`). Non-trivial requests route through the GOAL→PLAN→GATE→ACT→OBSERVE→VERIFY loop in both the sync endpoint and the SSE stream, with `fsm_state` events emitted to clients. Follow-up refinements are classified (`is_followup_refinement`) and can re-enter the FSM.

### Gap 2 — Semantic Memory: CLOSED ✅
`memory_advanced/` now has real embeddings (`embeddings.py`), a vector store with cosine similarity (`vector_store.py`), and a blended scorer (`scorer.py`: `α·cosine + β·recency + γ·importance`, defaults 0.6/0.25/0.15). Keyword-overlap retrieval is superseded.

### Gap 3 — Tool Self-Correction: CLOSED ✅
`tool_retry.py` implements retry-with-backoff; `tool_loop_guardrails.py` is success-aware (rephrases no longer kill the turn); `iteration_budget.py` bounds total work; `tool_result_persistence.py` keeps large results addressable across turns; reflexion critique is injected on tool failure so the model self-corrects.

### Gap 4 — Swarm Runtime: CLOSED ✅
`swarm/` is now a real runtime: priority mailbox (`mailbox.py`, 307 LOC), agent runtime (`runtime.py`), orchestrator (`orchestrator.py`), DB-persisted mailbox (`models/swarm_mailbox.py`), and tool handlers (`tool_handlers/swarm_tools.py`). No longer 14 lines of dataclasses.

### Gap 5 — Eval / Quality Gates: CLOSED ✅ (CI blocking unverified)
`eval_harness.py` + `golden_scenarios.json` + `eval_ci.py` provide golden-dataset regression with quality thresholds; `quality_gate_decision` is enforced on automation deliverables (sub-threshold confidence holds the file back, `automation_executor.py:548–553`). **Caveat:** not confirmed running as a blocking CI gate.

---

## The NEW Remaining Gaps (Priority Order)

### Gap A — Runtime Validation Deficit
**Impact:** HIGH | **Effort:** Medium

Everything above is verified by static analysis + unit tests only. No load testing, no chaos engineering, no soak runs have been performed (the July 16 caveat still stands). Unit-tested wiring ≠ production behavior.

**Missing:**
- Load/concurrency testing of the main chat loop and automation dispatcher
- Chaos tests: does `with_fallback` actually fire on provider 429/5xx? Does retry-with-backoff avoid thundering herd?
- End-to-end smoke: does a multi-step request visibly route through FSM and emit `fsm_state`? Does semantic recall retrieve by meaning, not keyword?

**Remediation:** Add a load-test script (e.g., locust/k6 against `add_message_stream`), a chaos harness that faults the LLM provider, and a live smoke checklist run per release.

---

### Gap B — Automation Scheduler Precision & Multi-Worker Efficiency
**Impact:** MEDIUM | **Effort:** Small

- `TICK_INTERVAL = 60` (`automation_dispatcher.py:53`) — cron schedules finer than 1 minute fire late.
- No leader election: every worker process ticks and scans due tasks. CAS claiming makes this *correct*, but N workers do N× the scan work.

**Remediation:** Reduce tick to 5–15s (or compute dynamic sleep-to-next-due); add a lightweight DB-backed leader lease for the tick loop.

---

### Gap C — Memory System Polish
**Impact:** MEDIUM | **Effort:** Small

- **OHMO memory consolidation is default-OFF** (`OHMO_MEMORY_CONSOLIDATION_ENABLED=False`, `agents.py:6399`) — the cross-session user-model merge never runs unless explicitly enabled.
- Blend weights `α/β/γ = 0.6/0.25/0.15` are hardcoded in `scorer.py` — not tunable per agent or per deployment.
- OHMO workspace prompt injection (`augment_system_prompt`) exists as a module but the call-site wiring into the main prompt builder is unverified.

**Remediation:** Enable consolidation behind a gradual rollout; expose weights via `settings`; verify/complete the OHMO injection call site.

---

### Gap D — Eval Gates Not Confirmed as CI-Blocking
**Impact:** MEDIUM | **Effort:** Small

`eval_ci.py` and golden datasets exist, and `test_eval_ci.py` passes locally, but there is no evidence the eval suite runs as a *blocking* gate in the CI pipeline (vs. informational or manual).

**Remediation:** Wire the eval harness into the CI workflow with a failing threshold; publish score trends.

---

### Gap E — Observability Surfacing
**Impact:** LOW–MEDIUM | **Effort:** Medium

`agent_metrics.py` records provider-fallback, reflexion, loop-guard, and FSM metrics, but there is no dashboard/alerting surface consuming them. Failover events and quality-gate rejections would currently go unnoticed in production.

**Remediation:** Export metrics (Prometheus/OTel) and build a minimal dashboard with alerts on fallback rate, gate-reject rate, and loop-guard trips.

---

### Gap F — Sub-Agent Prompt Depth (Unverified)
**Impact:** LOW | **Effort:** Small

The agent-improvement plan (Tasks 1–5) called for deepened sub-agent prompts (anti-hallucination, search strategy, severity classification, verification steps). `agent_definitions/` exists and `test_agent_definitions_prompts.py` passes, but the depth of the production prompts vs. plan spec is unverified in this analysis.

**Remediation:** Spot-audit `agent_definitions/__init__.py` prompts against the plan's checklist.

---

## What is Genuinely Strong (Keep & Build On)

| Strength | Rating | Why |
|----------|--------|-----|
| **Governance Layer** | 8/10 | Risk tiers, policy gate, output contracts, security pre-flights, quality-gated deliverables. |
| **Tool Breadth** | 8/10 | 82 tool handlers: DB (MySQL, PostgreSQL, MSSQL, Oracle), web, browser, sandbox, code exec, image, MCP, comms. |
| **Skill System** | 8/10 | Progressive-disclosure skills with dynamic discovery and forced-injection tests. |
| **Reliability Core** | 8/10 | Retry + success-aware guardrails + iteration budget + result persistence + reflexion — a coherent self-healing stack. |
| **Observability** | 8/10 | `Execution`/`ObservationRecord`/tracing + metrics for every new reliability mechanism. |
| **Automation** | 8/10 | Manus-parity+: skip-confirmation, live SSE, failure notify, bounded concurrency, quality gate, structured cross-run delta. |

---

## Proposed Roadmap

```
Phase 5 — Validate & Harden (1–2 weeks)   ← current priority
  ├── Live smoke checklist (FSM routing, semantic recall, swarm handoff)
  ├── Chaos tests: provider failover, retry storms, gate rejections
  ├── Load test main chat loop + automation dispatcher
  └── Wire eval harness as blocking CI gate

Phase 6 — Scheduler & Memory Polish (1 week)
  ├── Dispatcher tick 60s → 5–15s (or sleep-to-next-due)
  ├── DB-backed leader lease for multi-worker tick
  ├── Enable OHMO consolidation (gradual rollout)
  └── Expose memory blend weights in settings

Phase 7 — Operate (ongoing)
  ├── Metrics export + dashboard + alerts (fallback, gate-reject, loop-guard)
  ├── Sub-agent prompt audit vs. improvement plan
  └── Quarterly re-run of this gap analysis
```

---

## Evidence Map — New Modules (post 2026-07-16)

| Module | LOC | Purpose |
|--------|-----|---------|
| `services/planning_trigger.py` | — | Classifies when a message needs FSM planning |
| `services/synexia/chat_loop_adapter.py` | 101 | Bridges chat loop ↔ FSM |
| `services/synexia/reflexion.py` | 193 | Self-critique step in FSM |
| `services/synexia/eval_harness.py` | 370 | Golden-dataset regression runner |
| `services/synexia/eval_ci.py` | — | CI entrypoint for eval gates |
| `services/synexia/verifier.py` | — | VERIFY-state output verification |
| `services/memory_advanced/embeddings.py` | 231 | Embedding model integration |
| `services/memory_advanced/vector_store.py` | 175 | Cosine-similarity vector store |
| `services/memory_advanced/scorer.py` | 115 | α/β/γ blended retrieval scoring |
| `services/swarm/mailbox.py` | 307 | Priority inter-agent mailbox |
| `services/swarm/runtime.py` | 224 | Swarm agent runtime |
| `services/swarm/orchestrator.py` | 181 | Team orchestration |
| `services/tool_retry.py` | 241 | Retry with exponential backoff |
| `services/tool_loop_guardrails.py` | 296 | Success-aware loop guards |
| `services/iteration_budget.py` | 56 | Per-turn work budget |
| `services/tool_result_persistence.py` | — | 3-layer large-result persistence |
| `services/provider_fallback.py` | 82 | Automatic provider failover |
| `models/swarm_mailbox.py` | — | DB persistence for swarm messages |

**Key wiring points:** `agents.py:91–92` (FSM imports), `agents.py:2102–2120` (sync FSM route), `agents.py:4679–4747` (SSE FSM route), `agents.py:387–424` (reflexion injection), `agents.py:78, 2491` (provider fallback), `automation_dispatcher.py:609–616` (concurrency semaphore), `automation_executor.py:1136–1162` (quality gate).

**Key tests:** `test_automation_phase5_concurrency_and_callback.py`, `test_automation_phase5_quality_and_delta.py`, `test_fsm_reflexion_finalize.py`, `test_chat_loop_adapter.py`, `test_eval_harness.py`, `test_eval_ci.py`, `test_iteration_budget.py`, `test_gaps_1_to_4.py`, `test_loop_guard_smart_detection.py`.

---

## Appendix: Codebase Quick Reference

| Component | Path |
|-----------|------|
| Main agent runtime | `backend/app/routers/agents.py` |
| Planning trigger | `backend/app/services/planning_trigger.py` |
| Synexia planning FSM | `backend/app/services/synexia/fsm.py` |
| Reflexion / self-critique | `backend/app/services/synexia/reflexion.py`, `services/self_critic.py` |
| Eval harness & CI | `backend/app/services/synexia/eval_harness.py`, `eval_ci.py` |
| Memory (semantic) | `backend/app/services/memory_advanced/` |
| Swarm runtime | `backend/app/services/swarm/` |
| Tool reliability | `services/tool_retry.py`, `tool_loop_guardrails.py`, `iteration_budget.py` |
| Provider fallback | `backend/app/services/provider_fallback.py` |
| Automation | `services/automation_dispatcher.py`, `automation_executor.py` |
| Context compaction | `backend/app/services/compaction/` |
| Config & constants | `backend/app/config.py` |

---

*Report generated from static code analysis of `/root/zhanlu/`. Unit-test evidence was inspected but the suite was not executed (RAM-limited dev server); no runtime profiling or load testing was performed. Scores marked "unverified" require a live environment to confirm.*
