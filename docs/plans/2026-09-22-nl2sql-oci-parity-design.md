# NL2SQL — OCI Semantic-Enrichment Parity Design

Date: 2026-09-22
Reference: https://blogs.oracle.com/cloud-infrastructure/enterprise-nl2sql-with-semantic-enrichments
Status: Approved by user (Full OCI parity + clarify-when-uncertain)

## Context

The Zhanlu NL2SQL pipeline (`backend/app/services/db/nl_answer_service.py`) already
implements most OCI stages: Table Retrieval, Schema Linking, Dialect-Specific
Generation, SQL Rewriting, and Validation/Refinement. A semantic store facade
(`services/db/semantic_store.py`, explicitly modeled on the OCI design) exists but
is consumed only by the execution gate. An exploration audit identified six gaps.

## User decisions

- **Scope**: Full OCI parity = P0 wiring gaps + agentic planner + visualization stage.
- **Ambiguity UX**: Ask when uncertain — proceed with semantic-layer defaults when
  confidence is high; emit a structured clarification (metric/time/slice options)
  when genuinely ambiguous, reusing the existing `clarify` tool in the agent loop.

## Gaps → Design

### P0-1 — Wire Query Log Retrieval (OCI online stage 2)
- `services/db/query_log_retrieval.py` is complete but never imported.
- Call `build_query_log_guidance()` inside `NLAnswerService._answer_impl` near the
  plan-memory block; inject guidance into the generation prompt via
  `services/nl2sql/context_builder.py`. Gated by existing
  `NL2SQL_QUERY_LOG_RETRIEVAL_ENABLED`.

### P0-2 — Offline workload mining (OCI offline "Context Mining")
- New table `query_workload_stat` (per-KB aggregates: frequent tables, common
  filter columns + representative literals, join pairs, outcome counts).
- New Celery beat task `mine_nl2sql_workload` aggregating `nl2sql_query_logs`.
  Gated by `NL2SQL_WORKLOAD_MINING_ENABLED`. Mined stats surface as
  `LEARNED_FROM_WORKLOAD` provenance through the semantic store facade.

### P0-3 — Auto-promote successful Q→SQL into few-shot examples
- After a verified-successful run (executed OK, non-empty rows, no repair loop),
  insert a candidate into `q_sql_examples` with `LEARNED_FROM_WORKLOAD`
  provenance; dedupe by SQL hash; per-KB cap. New flag
  `NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED` (default False).

### P0-4 — Semantic-store-driven generation
- Route table retrieval and the generation context through `semantic_store.py`
  (glossary, metric definitions, aliases, join hints, workload stats) instead of
  reading catalog rows directly. One shared semantic layer feeds all stages.

### P1-5 — LLM planning stage + clarify gate (OCI agentic mode)
- New `services/db/nl2sql_planner.py` invoked at the top of `_answer_impl`:
  - Intent classification (metric / trend / comparison / detail).
  - Entity → glossary/metric-definition resolution; time → fiscal +
    data-refresh-aware range; user scope.
  - Structured plan recorded in the grounded plan record.
  - Clarify gate: low confidence AND `clarify` tool available → emit structured
    options; otherwise proceed with defaults and surface assumptions in narration.
- Flag `NL2SQL_PLANNER_ENABLED` + `NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED`.

### P1-6 — Multi-turn planning state
- Persist resolved entities / accepted defaults per conversation so follow-up
  turns narrow retrieval instead of restarting (extend `query_plan_memory`).

### P2-7 — Visualization stage
- New `services/db/chart_suggestor.py`: deterministic chart-type inference from
  result shape (time+measure → line; dim+measure → bar; 2 dims → stacked;
  ≥3 measures → table/heatmap). Adds a `visualization` field to the answer
  payload. Flag `NL2SQL_CHART_SUGGESTION_ENABLED`.

## Conventions
- All new flags default False; gate via `getattr(settings, "FLAG", False)`.
- `from app.config import settings`; Alembic migration for new tables.
- Limited-RAM environment: no parallel pytest; run tests in container or local venv.
- Backend redeploy: `docker restart zhanlu-backend zhanlu-sandbox-worker`.
