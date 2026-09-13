# Deep Research Mode — Design (v1)

Date: 2026-09-01 · Status: approved · Scope: backend orchestrator + chat-mode UI

## 1. Product decisions (brainstorming-approved)

| Question | Decision |
|---|---|
| Research scope | **Hybrid, source-scoped** — user picks `Web` / `Internal Data` / `Both` per run |
| Entry point | **Dedicated chat mode** + depth selector (Quick / Balanced / Deep) |
| Output | **Cited long-form report in chat** + Export (docx/pptx) |
| Scope picker | **Inline chips in composer** when Deep Research mode is on |
| Internal sources | **Current agent's bound sources only** (bound DBs/Ecisco BI + KBs + uploaded files) |
| Architecture | **Approach A** — in-loop research protocol (no async worker in v1) |

## 2. UX

- Composer: Deep Research toggle + depth selector + scope chips (default `Both`).
- Message metadata: `{research: {enabled: true, depth: "balanced", scope: "both"}}`.
- Progress streams via existing `LiveActivityStream` (`phase_enter.*` + `plan_step_*`).
- Final message: cited report with sections
  (Executive Summary / Methodology / Findings & Evidence / Data tables / Risks & Caveats / Sources)
  + Export button → docx/pptx via `artifact_service.create_artifact`.

## 3. Backend pipeline

New `backend/app/services/deep_research/orchestrator.py`:

- `DeepResearchRequest(question, depth="balanced", scope="both")` — validated.
- `run_deep_research(request, *, planner, tool_executor, synthesizer, critic, emit)` —
  async generator/orchestrator with injectable callables so tests use fakes and
  `agents.py` wires real `llm_service` + tool-registry implementations.
- Protocol (depth-scaled):
  1. `plan` — planner LLM builds research plan steps (question + tools per step);
     each step → `plan_step_added`.
  2. `gather` — 1–3 rounds; parallel tool fan-out per step:
     - Web scope → `web_search` / `web_extract`
     - Internal scope → `sql_query` / `ask_data_agent` / `comprehensive_data`
       (bound sources only, isolation preserved)
  3. `critique` — gap analysis LLM (Quick skips, Balanced = 1, Deep = 2);
     gaps become the next gather round's focus.
  4. `finalize` — cited synthesis via `citation_grounding`
     (`citation_instruction()` in prompt, `parse_citations` / `resolve_citations`
     on the result, sources section rendered).
- Phase events emitted: `phase_enter.research_plan`, `phase_enter.research_gather`,
  `phase_enter.research_critique`, `phase_enter.research_finalize`.
- Depth presets:

| Depth | gather rounds | critique rounds | max plan steps |
|---|---|---|---|
| quick | 1 | 0 | 4 |
| balanced | 2 | 1 | 6 |
| deep | 3 | 2 | 8 |

- Config (all default False / conservative):
  `DEEP_RESEARCH_ENABLED=False`, `DEEP_RESEARCH_MAX_ROUNDS=3`, `DEEP_RESEARCH_TIMEOUT_S=90`.
- `agents.py` hook: turn metadata with `research.enabled` → route to research
  protocol instead of default loop (flag-gated).

## 4. Data & persistence

- v1: **no new tables** — report is the assistant message; live events persist
  as today; export artifacts use existing storage. History/retry = v2 candidate.

## 5. Out of scope (v2 candidates)

- Background async research worker (long-run isolation).
- Research-run history table + retry/resume.
- Deep-lite auto-trigger (intent detection).

## 6. Test strategy (TDD)

- Unit: depth presets, request validation, scope→tool mapping, phase event
  sequence, critique round counts, citation parsing on final report.
- Wiring (AST): `agents.py` routes `research.enabled` metadata through
  `run_deep_research`; config flag gates it.
