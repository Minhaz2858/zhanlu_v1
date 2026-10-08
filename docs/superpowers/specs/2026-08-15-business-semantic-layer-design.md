# Business Semantic Layer — Design Spec

> Status: Approved (Approach A)
> Date: 2026-08-15
> Owner: Acme BI agent (project-based)

## 1. Problem

The "Acme BI" project agent fails at CEO-level business questions. Two observed failure traces:

1. **"Give me my supply chain data for last 15 days"** — the agent ran `MIN/MAX/COUNT` over the full-year table (196.3s), returned 1,387 records for the whole year instead of the 15-day window, and produced a defensive `DATA WARNING` artifact. The correct CEO answer was: *"no data after 2025-12-31 — ERP sync may be broken."*
2. **"Make a weekly market report"** — 780.8s, 14 steps, a good C5/C9 report artifact was produced, but the final chat message was the generic fallback *"I gathered some information but had trouble putting it all together…"*

Root causes are distinct:

- **Failure 1** is a *coverage/freshness* gap: the agent has no cheap way to know a table's data ends at `2025-12-31`, so it scans the whole table and counts rows instead of answering the temporal question.
- **Failure 2** is an *artifact-aware fallback* gap: `agents.py` falls back to a generic empty-content string whenever `assistant_content` is empty, even when an artifact was successfully produced.
- Both are compounded by a *narration* gap: `_NARRATE_SYSTEM_PROMPT` is a generic data-analyst persona with no executive output contract, so the agent drifts into meta-language ("snapshot", "payload") and raw record counts.

## 2. Solution Overview

A **Business Semantic Layer** that sits on top of the existing project knowledge-graph catalog (`kb_table_meta` / `kb_column_meta` / `schema_linker`, recall@10 = 0.82). It adds:

1. **Coverage metadata** — per-table `coverage_json` (`{date_column, min_date, max_date, probed_at}`), probed at index time (5s timeout) and refreshed nightly.
2. **Project metric registry** — a new curable `project_metric` table with human approval gate.
3. **Freshness short-circuit** — a deterministic relative-date parser resolves the question's time window; if all relevant tables' `max_date < window start`, answer in ~1s without querying.
4. **Business-context injection** — matched approved metrics + coverage annotations injected into `_SQL_GEN_SYSTEM_PROMPT` (progressive disclosure, few hundred tokens).
5. **Executive narration contract** — rewrite `_NARRATE_SYSTEM_PROMPT` to `HEADLINE → KEY FIGURES → CAVEAT (1 line) → NEXT STEP`; ban meta-language; never surface raw record counts as the answer.
6. **Artifact-aware fallback** — at `agents.py` post-loop fallback, build the message from artifact title+summary when an artifact was produced but `assistant_content` is empty.
7. **Validation hints** — advisory hints attached to `execute_query` results (date-range match, empty-set vs coverage, NULL rate); max 2 retries.
8. **Metrics curation UI** — a Metrics tab in the Data Map UI (list/approve/edit).

The structural catalog already exists and is **extended, never rebuilt**. This layer also activates the dormant `ProjectCatalogOverlay.metric_definition` path (currently written but never read at query time).

## 3. Design Principles

- **Extend, never rebuild.** The catalog (recall@10 0.82) is retained; this layer composes on top.
- **Prompts stay domain-free.** All domain content (C5/C9 patterns, 材料丙/开工率 vocabulary) lives in curable `project_metric` data rows, never hardcoded in prompts.
- **Progressive disclosure.** Only matched metric definitions + coverage are injected per question — never the whole playbook (a few hundred tokens).
- **Deterministic over LLM where possible.** Relative-date parsing is a pure function — testable, zero latency, no hallucination.
- **Human approval gate.** LLM-bootstrapped metrics land as `status='proposed'`; only `approved` rows are injected. Prevents LLM garbage from steering SQL.
- **Hints, not enforcement.** Validation hints are advisory fields on the tool result; we do not hard-block queries.
- **All new flags default `False`.** Zero behavior change when off.

## 4. Data Model

### 4.1 `kb_table_meta.coverage_json`

New JSON column (migration 052):

```json
{
  "date_column": "shipment_date",
  "min_date": "2025-01-01",
  "max_date": "2025-12-31",
  "probed_at": "2026-08-15T08:00:00+00:00"
}
```

- `date_column` — the best candidate date/timestamp column (determined by column-name heuristics + type).
- `min_date` / `max_date` — the observed range (`MIN(col)`, `MAX(col)`).
- `probed_at` — when the probe ran (ISO-8601). `null` if no date column exists or probe failed.
- Probing is **timeout-capped (5s)** and mirrors the existing `_safe_row_count` pattern in `catalog_indexer.py`.

### 4.2 `project_metric` table

New table (migration 052):

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `project_id` | UUID | scoped to a project |
| `kb_id` | UUID FK | the knowledge base (data source) this metric binds to |
| `name` | str | canonical name (e.g. "Gross Margin") |
| `aliases` | JSON list | `["毛利率", "gross margin", "margin %"]` |
| `definition` | Text | business definition |
| `sql_expression` | Text | canonical SQL expression (measure) |
| `query_pattern` | Text | reusable SQL template for this metric |
| `unit` | str | e.g. `%`, `CNY`, `tonnes` |
| `default_aggregation` | str | `sum` / `avg` / `max` / `min` / `count` |
| `bindings` | JSON list | `[{table, measure_columns, date_column, dimensions}]` |
| `source` | str | `llm` (bootstrapped) \| `user` (hand-curated) |
| `status` | str | `proposed` \| `approved` |
| + tenant/owner cols | | `org_id`, `app_id`, `created_by_id`, `is_deleted`, timestamps (via `TimestampedBase`) |

**Approval gate:** bootstrap lands `status='proposed'`; only `approved` rows are injected at query time. A metric may be promoted via the API (PUT `status=approved`).

## 5. Query-Time Flow

```
question
  → freshness pre-check  (KG_FRESHNESS_SHORTCIRCUIT_ENABLED)
      ├─ window > all relevant max_date → plain stale-data answer (~1s)
      └─ ok / unknown
  → business context     (KG_BUSINESS_CONTEXT_ENABLED)
  → schema_linker slice + coverage annotation
  → SQL gen with injected metrics + coverage
  → execute_query + validation hints
  → executive narration contract
```

### 5.1 Freshness pre-check (deterministic)

`parse_relative_window(question, today) -> (start, end) | None` resolves:

- `"last N days"` / `"过去 N 天"` → `[today - N, today]`
- `"last N weeks/months"` / `"本周/上周/本月/上月"` → window boundaries
- `"this week"`, `"YTD"`, `"今年"`, `"去年"` → calendar boundaries
- dialect-agnostic: returns resolved ISO dates, **not** a Postgres-only `INTERVAL` expression (avoids the mistake in the user's draft).

`freshness_verdict(kb_id, window, db)`:

- Loads relevant tables' `coverage_json.max_date`.
- If **all** tables have `max_date < window.start` → `{stale: True, max_date, message}`; `answer()` returns a plain-language stale-data statement in ~1s (no SQL).
- Otherwise `{stale: False}` — proceed.

### 5.2 Business context injection

`build_business_context(project_id, kb_id, question, db) -> str`:

1. Alias-match the question against `project_metric.aliases` (token overlap / CJK substring, reusing the linker's weighting).
2. Collect **approved** metrics only.
3. Attach coverage annotations from `kb_table_meta.coverage_json`.
4. Return a token-capped block (~300–500 tokens) injected into `_SQL_GEN_SYSTEM_PROMPT`.

### 5.3 Executive narration contract

`_NARRATE_SYSTEM_PROMPT` is rewritten:

```
HEADLINE     — one actionable business insight.
KEY FIGURES  — 2-4 supporting figures with units.
CAVEAT       — one line: data coverage / confidence.
NEXT STEP    — one recommended follow-up.
```

Hard rules:

- Never use meta-language: "snapshot", "payload", "as of the data", "the following".
- Never surface raw record counts as the answer ("1,387 rows" is not an insight).
- Every figure must be traceable to SQL evidence.

### 5.4 Validation hints

Structured advisory hints attached to the `execute_query` result (computed from already-returned rows + cached coverage lookup — O(rows), no extra queries):

- **date-range match** — did the resolved window match `coverage_json` range?
- **empty-set vs coverage** — empty result but `max_date` is recent vs genuinely no data.
- **NULL rate** — % of NULLs in key measure columns.

The agent reads these and may retry (≤2). Hints are advisory — they do not block when flags are off.

## 6. Bug Fixes

### 6.1 Artifact-aware fallback (`agents.py`)

Location: fallback chain at ~8033–8041, `else` branch (content_streamed → `_loop_guard_user_facing` → dashboard-specific → generic).

Change: when `assistant_content` is empty but an artifact was produced (`_orch_created` non-empty, available at ~8050), build the fallback from the artifact's `title` + `summary` instead of `_GENERIC_EMPTY_CONTENT_FALLBACK`.

- The dashboard-specific and loop-guard branches above it are **untouched**.
- Orchestrator-created artifacts (`_orch_created`) are in scope.

### 6.2 Freshness short-circuit prevents the full-year scan

Covered in §5.1 — this is the structural fix for Failure 1.

## 7. Flags (all default `False`)

| flag | purpose |
|---|---|
| `KG_BUSINESS_CONTEXT_ENABLED` | inject metrics + coverage into SQL-gen prompt |
| `KG_METRIC_BOOTSTRAP_ENABLED` | LLM-propose `project_metric` rows at index time |
| `KG_COVERAGE_PROBE_ENABLED` | probe `min_date`/`max_date` during indexing |
| `KG_FRESHNESS_SHORTCIRCUIT_ENABLED` | short-circuit stale-window questions |

Migration `052`. (051 is reserved by the CEO morning-digest plan.)

## 8. Frontend

Extend the Data Map UI (`DataMapSection.jsx`) with a **Metrics tab**:

- List `project_metric` rows (name, unit, status badge, source badge).
- Approve / reject (promote `proposed → approved`).
- Edit sheet (reuse existing `Sheet` / `Badge` / `Input` / `Textarea` patterns).
- Bootstrap button (calls `POST .../metrics/bootstrap`).
- All strings via `t.dataMap.metrics.*` (en/zh).

## 9. Phasing

| phase | scope |
|---|---|
| **1** | Both bug fixes: artifact-aware fallback + executive narration rewrite |
| **2** | Coverage probe + freshness short-circuit |
| **3** | Metric bootstrap + curation UI + business-context injection + `query_pattern` + validation hints |

## 10. Out of Scope / Deferred

- True video transcription (ffmpeg dependency) — separate follow-up.
- Vision-tool auto-description at ingestion time — separate task.
- Replacing the structural catalog or the Acme ontology port (Approach B / C) — rejected.
- Domain few-shots in global prompts — rejected (platform convention is domain-free).

## 11. Acceptance Criteria

- [ ] "last 15 days" question with stale data returns a plain-language stale answer in <2s (no full-year scan).
- [ ] Report artifact + empty assistant_content yields a message derived from artifact title/summary, never the generic fallback.
- [ ] Narration follows HEADLINE → KEY FIGURES → CAVEAT → NEXT STEP, with zero meta-language and zero raw record counts.
- [ ] Only `approved` metrics are injected; `proposed` metrics are never used at query time.
- [ ] All 4 flags default `False`; enabling one produces no crash when others are off.
- [ ] Metrics tab lists/approves/edits metrics; access scoping (tenant/owner) enforced.
- [ ] Targeted pytest files pass; router import verified; frontend rebuilt via `npx vite build --emptyOutDir=false`.
