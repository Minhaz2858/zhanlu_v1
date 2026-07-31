# Automation Response Quality — Grounding + Rich Synthesis + Adaptive Planning

**Date:** 2026-07-29
**Status:** Approved (design phase) — user selected "All three as a phased push" with
"Heavy: + adaptive mid-execution re-planning" for Phase 3.
**Scope:** Three phases with a fixed dependency order. Phase 1 = ground the FINALIZE
response prompt in real observation data + acceptance criteria. Phase 2 = wire the
task-aware `report_synthesis.py` into the FSM synthesize path. Phase 3 = plan-arg
population, multi-observation aggregation, and adaptive mid-execution re-planning.

**Approach:** Layered enhancement — each phase is an independently deployable unit
with its own feature flag. Phases have a dependency order (1 → 2 → 3) but each
delivers standalone value and can be rolled back independently. No big-bang change.

**Continuity:** This is the quality-side companion to the reliability work shipped in
`2026-07-29-automation-quality-reliability-design.md` (P0 retry/failover/compaction,
fail-closed permissions, live hooks, arg validation). That work made runs *reliable*;
this work makes their responses *specific and grounded*.

**Explicit non-goals (deferred):** cross-session learning, NL2SQL query plan caching,
multi-agent orchestration, A/B comparison of response variants, frontend streaming of
structured `ReportCardPayload` parts. The `agents.py` 305 KB modularization and the
conv-history JSON-blob → message-table refactor remain deferred (separate spec sessions).

---

## 1. Root cause & confirmed gaps (evidence)

All three phases share one root cause: **the response generator and the FSM synthesizer
write blind.** They know tools *ran* but not what they *found*, and the response
generator never sees the success criteria it is being judged against.

| # | Gap | Evidence |
|---|-----|----------|
| G1 | Response prompt is data-blind | `_build_response_prompt` (`fsm.py:839-861`) reduces each observation to a one-line summary: `✓ {tool}: {result_text or 'completed'}`. For an nl2sql node `result_text` is just `"Generated SQL: SELECT …"` — **no rows, no aggregate values**. For a sandbox node it is `"Generated DOCX artifact abc123"`. The generator knows tools ran but not what they returned. |
| G2 | Acceptance criteria invisible to the generator | `acceptance_criteria` (added to `task_spec` per `task_spec_parser.py:182`) are seen **only** by `QUALITY_EVAL` (`quality_eval.py:84-85`, via `_format_acceptance_criteria` at `:190`). The FINALIZE generator never sees them, so it doesn't know what to aim for, and QUALITY_EVAL's corrective loop has to fix avoidable gaps (more iterations = more cost). |
| G3 | Rich synthesis not wired to the FSM path | The sophisticated `report_synthesis.synthesize_report` (task-aware KPIs/charts/insights, anti-hallucination rules, top-12 aggregation, `FinalizeResult` payload) runs in the **v2 tool loop** only. The FSM path uses the simpler `_execute_synthesize_node` (`capability_router.py:389`) — a plain LLM call producing a JSON blob. FSM-path data tasks miss the structured `ReportCardPayload`. |
| G4 | Plan nodes carry no concrete arguments | `PlanNodeSpec.inputs` **exists** (`contracts.py:75`, default `{}`) but `generate_plan` (`plan_dag.py:22`) emits nodes with `name`/`description`/`dependencies` only — `inputs` stays empty. Tool nodes execute with args `{}`; only the specialized executors (nl2sql/synthesize/sandbox) derive inputs from the task spec ad hoc. |
| G5 | Synthesize reads only the last observation | `_get_previous_observation` (`capability_router.py:371-386`) orders by `seq.desc().first()` — only the most recent obs. Multi-step data retrieval (e.g. two nl2sql nodes) cannot be aggregated into the synthesis; earlier observations are dropped. |

**Why Phase 1 first:** grounding + criteria-awareness alone makes the initial response
data-grounded, so QUALITY_EVAL's corrective loop starts from a far better baseline —
fewer iterations, lower cost, faster runs. It *amplifies* the reliability work already
shipped, at zero extra LLM calls.

---

## 2. Design decisions (locked from brainstorming)

| Decision | Choice | Rationale |
|---|--------|----------|
| Phasing | **Layered, independently deployable** | Each phase ships standalone value and rolls back independently. A single integrated "quality engine" would be a big-bang change, hard to test and roll back. |
| Grounding token budget | **Cap block to ~2000 chars** (env `SYNEXIA_GROUNDING_MAX_CHARS`), truncate rows first | Keeps the response prompt bounded; columns/SQL/aggregate stats are higher-signal-per-char than raw rows, so rows are truncated before metadata. |
| Grounding extraction | **Per-obs-type extractor in a new module** (`grounding_extractor.py`) | Deterministic, no LLM call, unit-testable. Keeps `_build_response_prompt` a thin prompt assembler. |
| Criteria injection | **"You must satisfy:" block in the response prompt** | Same rendering already used by QUALITY_EVAL (`_format_acceptance_criteria`); reuse it so generator and evaluator share one definition of success. |
| Rich synthesis integration | **Replace `_execute_synthesize_node` body with sync-wrapped `synthesize_report`** via existing `_run_coro_sync` (`capability_router.py:248`); keep current simple LLM call as the fallback on exception | Reuses proven code rather than duplicating; fallback preserves today's behavior if synthesis raises. No new code path risk. |
| Plan-arg population | **Extend the planner LLM prompt to emit `inputs` per node**; store on `PlanNodeSpec.inputs` (field already exists) | Single source of truth for args; specialized executors continue to merge/override from task_spec for backward compat. |
| Multi-obs aggregation | **New `_get_all_data_observations`**; synthesize aggregates all nl2sql/data obs | No behavior change for single-obs plans (the common case); unblocks multi-step retrieval. |
| Adaptive re-planning | **Opt-in, default OFF**, feature-flagged `SYNEXIA_ADAPTIVE_PLANNING_ENABLED`; max revisions `SYNEXIA_ADAPTIVE_MAX_REVISIONS` (default 2) | Heavy scope adds LLM calls per checkpoint; default-off guarantees zero cost/latency change for existing runs until validated. |
| Adaptive decision surface | **`proceed` \| `insert_nodes` \| `modify_remaining` \| `complete_early`** | Matches the corrective-plan affordance already in `generate_plan` (`failure_context` param at `plan_dag.py:29`) — reuses the corrective-planning path rather than inventing a parallel one. |
| Checkpoint-eligible nodes | **`nl2sql`, `synthesize`, `sandbox`** (terminal-ish, produce data/artifacts) | Re-planning after every `tool` node would be noisy and costly; checkpoints only after nodes that materially change what the plan should do next. |
| New dependencies | **None** | RAM-limited dev box; matches lazy/optional dependency policy. |
| Schema changes | **None** — `Plan` already has `plan_version`; adaptive revisions stamp version increments | Project convention (runtime `ALTER TABLE`, no Alembic); no new columns needed. |
| Testing | **TDD per phase** (RED → GREEN), explicit test-file paths only, no `-n` workers | RAM constraint; same discipline as the reliability work. |

---

## 3. Phase 1 — Response Grounding + Criteria-Aware Generation (fixes G1, G2)

### 3.1 Grounding extractor (new module)

**New module:** `backend/app/services/synexia/grounding_extractor.py`

A pure, deterministic extractor that distills each observation type into a compact,
token-bounded block. No LLM calls.

```python
def extract_grounding(
    observations: list[ObservationRecord],
    *,
    max_chars: int | None = None,  # default SYNEXIA_GROUNDING_MAX_CHARS (2000)
) -> str
```

Per-obs-type extraction (each returns a compact string):

| Obs type | Grounding extracted |
|---|---|
| `nl2sql` | SQL (truncated to ~400 chars) + row count + column names + **top-3 rows** + aggregate stats (sum/min/max of numeric columns) |
| `synthesize` | KPIs (`label: value`) + insights + chart title (read from `result_data`) |
| `sandbox` | artifact type + title + format (read from `result_data`) |
| `tool` / other | result summary truncated to ~500 chars |

**Token budget:** the assembled block is capped to `max_chars`. Truncation order:
rows first (drop to top-3, then top-1), then per-obs result text, then SQL length —
metadata (counts, columns, aggregates) is never dropped before data. Each observation's
contribution is prefixed with its node/seq so the generator can attribute findings.

**Defensive:** all field reads use `.get()` / `getattr` with fallbacks — observation
objects may be partial (tests, detached executions), matching the existing defensive
style in `_build_response_prompt` (`fsm.py:853-855`).

### 3.2 Criteria-aware response prompt

**Where:** `fsm.py:_build_response_prompt` (`:839`).

- Replace the thin `obs_summaries` loop (`:850-861`) with a call to
  `extract_grounding(self.execution.observations)`, producing a structured
  `=== Findings (from executed actions) ===` block with real data.
- Inject an `=== You must satisfy ===` block rendering the task's
  `acceptance_criteria` via the **existing** `quality_eval._format_acceptance_criteria`
  helper (`quality_eval.py:190`) — reused verbatim so the generator and the evaluator
  share one definition of success. Falls back to `kpis` when criteria are absent
  (same fallback QUALITY_EVAL already uses).
- The guidance sentence in the prompt becomes: *"Your response must reference the
  actual findings below by value, and must satisfy every criterion listed. Do not
  narrate tool calls; report the results."*

**Shared path:** this prompt is already shared by the blocking `_generate_response`
and the streaming `stream_final_response` (see docstring at `fsm.py:842-848`), so both
paths get the grounding automatically.

### 3.3 Config

- `SYNEXIA_GROUNDING_MAX_CHARS` (default `2000`) — grounding block char cap.

**Cost:** 0 extra LLM calls.

---

## 4. Phase 2 — Rich Synthesis for the FSM Path (fixes G3)

### 4.1 Wire `synthesize_report` into the FSM synthesize node

**Where:** `capability_router._execute_synthesize_node` (`:389`).

- Replace the body's plain `call_llm` (`:442-448`) with a sync-wrapped call to
  `report_synthesis.synthesize_report` (async, signature at `report_synthesis.py:598`)
  using the existing `_run_coro_sync` bridge (`capability_router.py:248`).
- Adapt the call: gather `rows`/`sql` from the preceding nl2sql observation (same
  `_get_previous_observation` read, `:400`), pass `source_name`/`source_id` from the
  task spec entities, pass `call_llm_fn` as a thin sync→async wrapper around `call_llm`.
- **Fallback:** wrap in try/except; on any exception, fall back to the current simple
  `call_llm` JSON-blob path (preserve today's behavior). Log the fallback reason.
- The resulting `FinalizeResult` is converted to the `ObservationRecord.result_data`
  shape the downstream sandbox node already expects (KPIs/chart/insights), so the
  sandbox executor needs no change.

### 4.2 Surface `ReportCardPayload` in FINALIZE

**Where:** `fsm.py` FINALIZE (where `report_card_payload` is assembled, `:832`).

- For non-file data tasks (`analyze_data` without export), surface the
  `ReportCardPayload` (KPIs/chart/insights) produced by Phase 2 in FINALIZE so the
  frontend renders structured KPIs/charts instead of plain text. The field already
  exists on the finalize result (`report_card_payload=...` at `:832`); this populates
  it from the FSM path instead of leaving it empty.
- File-export tasks keep current behavior (sandbox builds the deliverable).

**Cost:** 0 extra LLM calls — replaces the existing synthesize call with a richer one
that produces the same kind of output, just better.

---

## 5. Phase 3 — Smarter Planning + Adaptive Re-planning (fixes G4, G5)

### 5.1 Plan-arg population (fixes G4; 0 extra calls)

**Where:** `plan_dag.generate_plan` (`:22`).

- Extend the planner LLM prompt to emit `inputs` per node (e.g. an nl2sql node gets
  `{"question": <from entities>}`; a sandbox node gets `{"title": ..., "format": ...}`).
- Parse `inputs` from the LLM's JSON and store on `PlanNodeSpec.inputs` (field already
  exists at `contracts.py:75`).
- **Backward compat:** specialized executors (nl2sql/synthesize/sandbox) continue to
  merge/override `inputs` from the task spec ad hoc — populated `inputs` is an
  *additional* source, not a replacement, so existing single-node plans are unaffected.

### 5.2 Multi-observation aggregation (fixes G5; 0 extra calls)

**Where:** `capability_router.py`.

- New `_get_all_data_observations(db, execution)` — returns **all** nl2sql/data
  observations for the execution, ordered by `seq` ascending (chronological), instead
  of only the most recent (`_get_previous_observation`, `:371`).
- `_execute_synthesize_node` (`:389`) and the sandbox executor (`:503-504`) switch to
  the new aggregator so multi-step retrieval is aggregated, not dropped.
- **No behavior change** for single-observation plans (the common case): the aggregator
  returns a one-element list and the existing single-obs rendering path is preserved.

### 5.3 Adaptive mid-execution re-planning (the heavy part; opt-in)

**New module:** `backend/app/services/synexia/adaptive_planner.py`

After each **checkpoint-eligible** node (`nl2sql`, `synthesize`, `sandbox`), the
adaptive planner:

1. Collects all accumulated observations + the remaining plan tail.
2. Calls the LLM with a structured prompt: review findings, decide
   `proceed` | `insert_nodes` | `modify_remaining` | `complete_early`.
3. On `proceed`/`complete_early`: no plan change (early-complete short-circuits to
   FINALIZE).
4. On `insert_nodes`/`modify_remaining`: returns an updated plan tail (new/modified
   `PlanNodeSpec`s). The FSM applies the tail by reusing the **existing corrective-plan
   affordance** — `generate_plan(failure_context=...)` (`plan_dag.py:29`) already
   supports corrective re-planning; adaptive inserts reuse this path, stamping
   `plan_version += 1` on the `Plan` row.

**Guardrails:**

- Feature flag `SYNEXIA_ADAPTIVE_PLANNING_ENABLED` (default **False**).
- Max revisions per run `SYNEXIA_ADAPTIVE_MAX_REVISIONS` (default `2`) — after the cap,
  the planner is skipped for the rest of the run (proceed).
- Checkpoint nodes only (`nl2sql`/`synthesize`/`sandbox`) — never after plain `tool`
  nodes (would be noisy/costly).
- Revision cost is bounded: ≤ `MAX_REVISIONS` extra LLM calls per run, typically 1–3.
- Fail-safe: if the adaptive call errors or returns an unparseable decision, fall back
  to `proceed` (the run continues on the original plan) — never abort a run on an
  adaptive-planning failure.
- **Non-overlap with QUALITY_EVAL:** adaptive re-planning acts **mid-run, proactively**
  (before FINALIZE) to redirect remaining steps; QUALITY_EVAL acts **post-run,
  correctively** (after FINALIZE) to fix the response text. They operate on different
  objects (the plan tail vs. the response text), so enabling adaptive does not duplicate
  QUALITY_EVAL's corrective work.

**Cost when disabled (default):** 0 extra calls. **Cost when enabled:** ≤ 1 call per
checkpoint, bounded by `MAX_REVISIONS`.

---

## 6. Feature flags & cost summary

| Phase | Flag | Default | Extra LLM calls |
|---|---|---|---|
| 1 Grounding + criteria | (always on) | on | 0 |
| 2 Rich synthesis | (always on) | on | 0 (replaces existing call) |
| 3a Plan args + 3b multi-obs | (always on) | on | 0 |
| 3c Adaptive planning | `SYNEXIA_ADAPTIVE_PLANNING_ENABLED` | **off** | ≤ `MAX_REVISIONS` per run (default cap 2) |

Supporting config: `SYNEXIA_GROUNDING_MAX_CHARS` (2000),
`SYNEXIA_ADAPTIVE_MAX_REVISIONS` (2).

---

## 7. Testing strategy

RAM-limited box: explicit test file paths only, sqlite test DB, no `-n` workers (see
backend conventions). TDD per phase (RED → GREEN). New tests, one file per concern:

| Test file | Covers |
|---|---|
| `tests/test_grounding_extractor.py` | Per-type extraction: nl2sql (SQL+rows+aggregates), synthesize (KPIs/insights/chart), sandbox (artifact meta), tool (truncation); char-cap truncation order (rows → text → SQL, metadata last); defensive reads on partial observations; empty observations |
| `tests/test_response_prompt_grounding.py` | `_build_response_prompt` includes the grounded findings block with real values; includes `You must satisfy` block from `acceptance_criteria`; falls back to `kpis` when criteria absent; shared by blocking + streaming paths |
| `tests/test_fsm_synthesize_rich.py` | `_execute_synthesize_node` calls `synthesize_report` via `_run_coro_sync`; produces `ReportCardPayload`-shaped `result_data`; falls back to simple LLM call on exception; FINALIZE surfaces `report_card_payload` for non-file data tasks |
| `tests/test_plan_args_population.py` | `generate_plan` emits `inputs` per node; inputs parsed from LLM JSON; stored on `PlanNodeSpec.inputs`; specialized executors still merge task-spec inputs (backward compat) |
| `tests/test_multi_obs_aggregation.py` | `_get_all_data_observations` returns all data obs chronologically; synthesize aggregates multi-step retrieval; single-obs plans unchanged; sandbox reads aggregated context |
| `tests/test_adaptive_planner.py` | `proceed` → no change; `complete_early` → short-circuit to FINALIZE; `insert_nodes`/`modify_remaining` → updated tail + `plan_version` increment; `MAX_REVISIONS` cap skips further calls; checkpoint-only (no re-plan after plain `tool`); error/unparseable → `proceed` fallback; **disabled by default** (no calls when flag off) |

Eval harness (additive): extend the existing graders with a "grounding" grader (does the
response cite actual observation values?) and a "criteria" grader (does it satisfy each
acceptance criterion?). Run against the regression corpus only — no live runs in CI.

---

## 8. Rollout & compatibility

- **Phase 1** ships first (zero calls, pure prompt enrichment); independently shippable
  and immediately improves every run's response quality.
- **Phase 2** ships next; replaces the FSM synthesize call with a richer one, with the
  simple LLM call preserved as fallback — no behavior regression if synthesis raises.
- **Phase 3a + 3b** ship together (zero calls, deterministic improvements).
- **Phase 3c** ships last, **default OFF**, opt-in via env. Validate on staging with the
  eval corpus before flipping the default.
- Each phase gets its own commit(s); no phase blocks on a later one. Backend changes
  require `docker restart zhanlu-backend` (reload is off to protect in-flight runs).

## 9. Follow-ups (tracked, not in this spec)

- Frontend streaming of structured `ReportCardPayload` parts (currently the payload is
  rendered as a single block).
- Cross-session learning (reuse grounding/insights across runs of the same task).
- NL2SQL query-plan caching to cut latency on repeated analyses.
- Multi-agent orchestration (isolated sub-agent contexts) — harness-audit P3 item.
- The `agents.py` 305 KB modularization and conv-history JSON-blob → message-table
  refactor remain deferred (separate spec sessions).
