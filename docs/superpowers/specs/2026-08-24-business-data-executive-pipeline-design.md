# Enterprise Business-Data Executive Pipeline — Design

**Date:** 2026-08-24 (revised)
**Status:** Revised per user direction — shifted from hardcoded keyword router to Dynamic Enterprise Orchestrator
**Author:** Pair-programming session

## 1. Problem Statement

When enterprise users ask business-data questions ("Why are gross margins
dropping in the Southern region despite rising sales?", "Give me supply chain
data for last 30 days", "Q3 customer churn by segment"), the agent today
returns thin, unusable artifacts:

- **Wrong table selection**: the schema linker (ChromaDB RRF, top-8) picks
  `erp_t_crm_contractentry` and emits a single `SUM(FQTY)`. The 1 row returned
  gets pivoted into 4 column headers (Total/Max volume/inventory) — every
  cell is the same number.
- **No multi-source fusion**: only one `ask_data_agent` call fires. The
  CEO-GRADE 6-SECTION prompt (`synexia/report_synthesis.py:264-357`) cannot
  write a multi-section narrative without multi-source data.
- **No domain adaptivity**: the existing CEO-GRADE prompt is supply-chain
  flavored ("Transmission Risk" section); a CFO asking about margin
  compression needs different section labels ("Cash-Flow Exposure").
- **No fact-checking / lineage**: synthesizer prose is not grounded in
  verifiable source rows; high-stakes decisions cannot trace a claim back
  to the SQL that produced it.
- **Title fallback grabs "Executive Summary"**: when the user message has
  no extractable period entity, `_generate_meaningful_title` falls through
  to `_title_from_prose`, which returns the first H1 ("Executive Summary").

The user's vision (this revision) is a **Dynamic Enterprise Orchestrator**
that handles ANY business domain (Finance, Logistics, Sales Ops, Risk, HR,
Procurement) — not just supply chain. The agent must dynamically discover
relevant DB entities, plan 3-6 facets, execute them in parallel, synthesize
a domain-adaptive 6-section report, and attach verifiable data lineage.

## 2. Root Cause (confirmed via code inspection)

| Layer | What's wrong | Code reference |
|---|---|---|
| Schema linker | RRF picks "most similar" table not the right one | `knowledge_graph/schema_linker.py:38-94` |
| Multi-source collection | Single `ask_data_agent` call, no facet fan-out | `tool_handlers/delegation_tools.py` |
| Domain adaptivity | CEO-GRADE prompt is supply-chain-flavored; sections are hardcoded | `synexia/report_synthesis.py:264-357` |
| Fact-checking | No claim-to-source-row mapping; prose is unverified | (no existing module) |
| Title fallback | `_title_from_prose` grabs "Executive Summary" as title | `generation_orchestrator.py:337-348, 434` |
| DOCX renderer | Renders the thin payload as-is; no 6-section template; no lineage appendix | `artifacts/exporters/docx_export.py:201+` |

**Critical reuse** (no new SQL needed):
- `app/services/erp/kpi.py::ErpKpiService` — sales/inventory/customer/order
  KPIs against `erp_v_sale_orderentry` (cols `FDELIQTY`, `PLANDATE`,
  `FREMAINOUTQTY`)
- `app/services/perception/service.py::PerceptionService.build_snapshot()`
  + `perception/impact.py::compute_impact()` — market events + naphtha
  pass-through
- `app/services/knowledge_graph/schema_linker.py` — Two-Phase NL2SQL already
  deployed (Phase A: LLM sees full TOC, picks 1-4 tables; Phase B: selected
  tables get full DDL → SQL gen). The Facet Planner REUSES this for
  ad-hoc facets.

## 3. Architecture — Dynamic Enterprise Orchestrator

```
USER ENTERPRISE QUERY
("Why are gross margins dropping in the Southern region despite rising sales?")
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 1 — Dynamic Resource & Intent Profiler                 │
│   (NEW: enterprise_orchestrator/profiler.py)                │
│                                                              │
│   • LLM call (single, structured-JSON output, NO tools):     │
│     INPUT:  user_message + cached schema slice               │
│     OUTPUT: {                                                │
│       domain: "financial_performance" | "supply_chain" |     │
│              "sales_operations" | "risk_management" |        │
│              "logistics" | "hr" | "procurement" | "generic", │
│       period: ("2026-07-25", "2026-08-23") | null,           │
│       primary_metric: "gross_margin_pct",                   │
│       segments: ["region=Southern"],                        │
│       facets: [                                              │
│         {                                                    │
│           facet_id: "regional_sales",                        │
│           kind: "service_call",                              │
│           service: "ErpKpiService.sales_summary_for_period",│
│           args: {period, segment_filter="region=Southern"},│
│           purpose: "primary"                                 │
│         },                                                   │
│         {                                                    │
│           facet_id: "regional_cogs",                         │
│           kind: "ad_hoc_query",                              │
│           natural_language: "COGS broken down by region for │
│             last 30 days, with material cost component",     │
│           suggested_tables: ["erp_t_sal_outstockentry",     │
│                              "erp_t_inv_inventory"],         │
│           purpose: "auxiliary"                               │
│         },                                                   │
│         ... (3-6 facets total)                                │
│       ]                                                      │
│     }                                                        │
│                                                              │
│   • Domain → section labels lookup table (deterministic):    │
│       supply_chain  → "Transmission Risk"                    │
│       financial     → "Cash-Flow Exposure"                   │
│       logistics     → "Service-Level Exceptions"             │
│       risk_mgmt     → "Concentration & Counterparty"         │
│       sales_ops     → "Pipeline & Forecast Risk"            │
│       generic       → "Risk Assessment"                      │
│                                                              │
│   • FAIL-OPEN: if LLM call fails or returns no facets,      │
│     fall back to existing single-query path (zero regression)│
└─────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 2 — Multi-Facet Parallel Query Execution               │
│   (NEW: enterprise_orchestrator/executor.py)                │
│                                                              │
│   asyncio.gather over the planned facets:                    │
│   • service_call facets → invoke the named service directly │
│     (ErpKpiService, PerceptionService)                       │
│   • ad_hoc_query facets → invoke ask_data_agent with the    │
│     natural_language + suggested_tables (reuses Two-Phase   │
│     NL2SQL: Phase A picks tables, Phase B generates SQL)    │
│                                                              │
│   Each facet returns a FacetResult:                          │
│     {                                                        │
│       facet_id, kind, rows, summary, source_sql,             │
│       source_label, row_count, warnings,                     │
│       available: bool, unavailable_reason: str,              │
│       execution_log: [{step, latency_ms, status, ...}]       │
│     }                                                        │
│                                                              │
│   Partial failures are isolated — one facet failing does not │
│   abort the others. Missing facets become explicit           │
│   "(unavailable: reason)" sections.                         │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 3 — Enterprise Synthesis & Cross-Facet Correlation     │
│   (NEW: enterprise_orchestrator/synthesizer.py)              │
│                                                              │
│   • Deterministic transforms: rank rows, compute shares,    │
│     deltas, concentration metrics. NO LLM call here.         │
│   • Claim extraction: every narrative sentence that cites a │
│     number MUST be tagged with source_facet + source_row_ids.│
│     E.g. "Revenue fell 18.0% QoQ" →                          │
│       {claim: "Revenue fell 18.0% QoQ",                     │
│        source_facet: "regional_sales",                       │
│        source_row_ids: ["row_3", "row_7"],                   │
│        source_sql: "SELECT SUM(...) FROM erp_v_..."}         │
│   • Recommended Actions: rules table (low-stock → restock,  │
│     margin_compression → pricing review, concentration >    │
│     60% → diversify, etc.)                                  │
│   • Adaptive 6-section payload (labels from the domain map):│
│     [Exec Summary | Primary Metric Breakdown | Segment      │
│      Decomposition | Operational Drivers & Anomalies |      │
│      {Domain-Specific Risk Section} | Strategic              │
│      Recommendations]                                       │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 4 — Dual Truth-Backed Rendering                         │
│                                                              │
│   A. docx.render_enterprise_report(payload)  (NEW)           │
│      • Cover page with title + period + source label         │
│      • 6 sections, each: H1 + table + narrative + claim      │
│        citations (small superscript [1], [2]…)               │
│      • Appendix: Data Lineage — for each facet, list the    │
│        SQL + execution log + row count                      │
│                                                              │
│   B. html_chat.render_enterprise_report(payload)  (NEW)      │
│      • Inline 6-section markdown for the chat bubble        │
│      • Same claim citations link to the appendix (rendered  │
│        as collapsed <details> at the end of the bubble)      │
│                                                              │
│   Both renderers consume the SAME fused payload — no drift. │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
        ▼  Title generation (FIX)
┌─────────────────────────────────────────────────────────────┐
│ TITLE FIX (generation_orchestrator.py)                       │
│   • _title_from_prose MUST NOT match generic section          │
│     headings ("Executive Summary", "Key Metrics", ...) —     │
│     adds _GENERIC_HEADING_BLOCKLIST                            │
│   • Enterprise reports: title = "{period} {domain_label}     │
│     Report" (e.g. "Last 30 days Financial Performance        │
│     Report") — never "Executive Summary"                     │
└─────────────────────────────────────────────────────────────┘
```

## 4. Components

### 4.1 NEW files

| File | Purpose |
|---|---|
| `backend/app/services/enterprise_orchestrator/__init__.py` | Package init |
| `backend/app/services/enterprise_orchestrator/profiler.py` | `profile_enterprise_intent(user_message, schema_slice, llm_caller) -> EnterpriseIntent \| None` — single LLM call with structured-JSON output, fails open |
| `backend/app/services/enterprise_orchestrator/domain_labels.py` | Domain → section label mapping table (deterministic; covers supply_chain, financial, logistics, risk_mgmt, sales_ops, hr, procurement, generic) |
| `backend/app/services/enterprise_orchestrator/executor.py` | `execute_facets(intent, db) -> dict[facet_id, FacetResult]` — async fan-out, isolates per-facet failures |
| `backend/app/services/enterprise_orchestrator/synthesizer.py` | `synthesize_enterprise_report(facets, intent) -> EnterpriseReport` — deterministic transforms + claim extraction + recommended-actions rules |
| `backend/app/services/enterprise_orchestrator/claim_tracker.py` | `ClaimTracker` — collects claim→source mappings; `verify_claims(report) -> VerificationResult` — rejects any claim lacking source rows |
| `backend/app/services/enterprise_orchestrator/renderers/__init__.py` | Renderer package init |
| `backend/app/services/enterprise_orchestrator/renderers/docx.py` | `render_enterprise_docx(payload) -> bytes` — 6-section DOCX + lineage appendix |
| `backend/app/services/enterprise_orchestrator/renderers/html_chat.py` | `render_enterprise_html(payload) -> str` — inline 6-section markdown with collapsed lineage `<details>` |
| `backend/app/services/enterprise_orchestrator/title_builder.py` | `build_enterprise_title(intent, facets) -> str` — "{period} {domain_label} Report" |
| `backend/app/services/enterprise_orchestrator/prompts.py` | The ENTERPRISE BUSINESS-DATA REPORT PROTOCOL system-prompt block (per user's spec) |
| `backend/tests/services/enterprise_orchestrator/test_profiler.py` | ~15 LLM-mock tests (intent classification, facet planning, fail-open) |
| `backend/tests/services/enterprise_orchestrator/test_executor.py` | ~10 executor tests (parallel fan-out, partial failure, execution log) |
| `backend/tests/services/enterprise_orchestrator/test_synthesizer.py` | ~25 fusion tests (deterministic transforms, claim extraction, recommended-actions rules, missing facets) |
| `backend/tests/services/enterprise_orchestrator/test_claim_tracker.py` | ~10 verification tests (ungrounded claim rejected, source mapping round-trips) |
| `backend/tests/services/enterprise_orchestrator/test_docx_renderer.py` | ~8 DOCX tests (6 sections present, lineage appendix, NO "Executive Summary" title) |
| `backend/tests/services/enterprise_orchestrator/test_html_renderer.py` | ~7 HTML tests (markdown structure, collapsed details, citations) |
| `backend/tests/services/enterprise_orchestrator/test_domain_labels.py` | ~5 mapping tests |

### 4.2 MODIFIED files

| File | Change |
|---|---|
| `app/services/agent_prompts.py` | Insert the ENTERPRISE BUSINESS-DATA REPORT PROTOCOL prompt block (see §13). Existing single-query path remains as fallback. |
| `app/services/tool_handlers/__init__.py` | Register the new `collect_enterprise_data` tool wrapper that calls profiler → executor → synthesizer → returns the EnterpriseReport payload |
| `app/services/generation_orchestrator.py` | Wire the new tool result into the artifact-creation path. When `collect_enterprise_data` was the data tool, route payload → `renderers.docx` for `docx` format, → `renderers.html_chat` for chat bubble. Fix `_title_from_prose` to reject generic headings via `_GENERIC_HEADING_BLOCKLIST`. |
| `app/services/artifacts/exporters/docx_export.py` | When payload has `enterprise_report_kind == "executive"`, delegate to `enterprise_orchestrator.renderers.docx.render_enterprise_docx(payload)`. Otherwise current renderer unchanged. |

## 5. Data Flow

```
1. user_message = "Why are gross margins dropping in Southern region
                   despite rising sales?"

2. agents.py v3 loop: agent sees the ENTERPRISE BUSINESS-DATA REPORT
   PROTOCOL prompt block, decides to call `collect_enterprise_data` tool:
   {user_message: <the query>, output_format: "docx" | "html"}

3. tool_handlers/enterprise_data_tools.py handles the call:
   a) Build schema slice (cached; reuse data_source_runtime._build_schema_slice)
   b) enterprise_orchestrator.profiler.profile_enterprise_intent(
        user_message, schema_slice, llm_caller=_fast_llm_call
      ) → EnterpriseIntent(domain, period, primary_metric, segments, facets)
      [single LLM call, ~10-20s on deepseek-chat; structured JSON output]
   c) enterprise_orchestrator.executor.execute_facets(intent, db)
      → asyncio.gather:
        - service_call facets → invoke named service directly
        - ad_hoc_query facets → invoke ask_data_agent (reuses Two-Phase NL2SQL)
      [per-facet timeout: 60s; partial failures isolated]
   d) enterprise_orchestrator.synthesizer.synthesize_enterprise_report(
        facets, intent
      ) → EnterpriseReport payload with claim citations
   e) enterprise_orchestrator.claim_tracker.verify_claims(report)
      → if any claim lacks source rows, replace with "Data unavailable
        for this claim" placeholder; do NOT silently drop
   f) Return EnterpriseReport payload to agents.py v3 loop.

4. agents.py deferred-finalize:
   - Title: enterprise_orchestrator.title_builder.build_enterprise_title(
       intent, facets
     ) → "Last 30 days Financial Performance Report"
   - DOCX: enterprise_orchestrator.renderers.docx.render_enterprise_docx(
       payload
     ) → 6-section .docx with lineage appendix
   - Chat bubble: enterprise_orchestrator.renderers.html_chat
     .render_enterprise_html(payload)
     → inline 6-section markdown with collapsed lineage
```

## 6. Dynamic Resource & Intent Profiler — Specification

### 6.1 LLM call

- **Model**: reuses the project's main LLM (DeepSeek `deepseek-chat` or
  whatever is configured). NO new model.
- **Mode**: single call, structured-JSON output. NO tool-calling,
  NO chain-of-thought reasoning. The prompt instructs the LLM to emit
  JSON only.
- **Temperature**: 0.0 (deterministic planning).
- **Max tokens**: 1500 (cap on facet count + descriptions).
- **Timeout**: 30s. On timeout → fail-open to existing single-query path.

### 6.2 Profiler prompt (system)

```
You are a business-data facet planner. Given a user's enterprise query
and the available database schema slice, output a JSON plan for a
multi-facet data-collection pipeline. Do NOT execute queries — just plan.

Output JSON schema:
{
  "domain": "financial_performance" | "supply_chain" | "sales_operations"
            | "risk_management" | "logistics" | "hr" | "procurement"
            | "generic",
  "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} | null,
  "primary_metric": "<one short label, e.g. 'gross_margin_pct'>",
  "segments": ["<filter_expr>", ...],   // e.g. "region=Southern"
  "facets": [
    {
      "facet_id": "<short_snake_case_id>",
      "kind": "service_call" | "ad_hoc_query",
      "service": "<fully.qualified.method>",   // only if kind=service_call
      "args": {<kwargs>},                        // only if kind=service_call
      "natural_language": "<NL query>",          // only if kind=ad_hoc_query
      "suggested_tables": ["<table_name>", ...], // only if kind=ad_hoc_query
      "purpose": "primary" | "auxiliary" | "contextual"
    }
  ]   // 3-6 facets total
}

Rules:
- Prefer service_call facets when the requested metric is covered by an
  existing service (ErpKpiService, PerceptionService). Use ad_hoc_query
  for everything else.
- Always include at least one "primary" facet for the main metric.
- For "why is X dropping" questions, include both the metric facet AND
  a decomposition facet (by region / product / customer).
- For margin/profit questions, include both revenue AND cost facets.
- For supply-chain questions, include inventory + market events facets.
- Output ONLY the JSON. No prose, no markdown fences.
```

### 6.3 Fail-open behavior

- LLM call timeout / 5xx → return `None`; caller falls through to
  existing single-query path.
- LLM returns malformed JSON → attempt `json.loads` repair (strip
  fences, extract first `{...}` block); if still fails, return `None`.
- LLM returns 0 facets or > 6 facets → clamp/return `None`.
- LLM emits `kind=service_call` for a non-existent service → the
  executor marks that facet as `available=False, unavailable_reason=
  "service not found"` and continues.

## 7. Domain → Section Label Mapping (deterministic)

```python
DOMAIN_SECTION_LABELS = {
    "supply_chain": {
        "risk_section": "Transmission Risk",
        "drivers_section": "Supply-Demand Drivers",
    },
    "financial_performance": {
        "risk_section": "Cash-Flow Exposure",
        "drivers_section": "Margin & Cost Drivers",
    },
    "logistics": {
        "risk_section": "Service-Level Exceptions",
        "drivers_section": "Throughput & Carrier Drivers",
    },
    "risk_management": {
        "risk_section": "Concentration & Counterparty",
        "drivers_section": "Exposure Decomposition",
    },
    "sales_operations": {
        "risk_section": "Pipeline & Forecast Risk",
        "drivers_section": "Funnel & Conversion Drivers",
    },
    "hr": {
        "risk_section": "Attrition & Capacity Risk",
        "drivers_section": "Workforce Composition",
    },
    "procurement": {
        "risk_section": "Supplier & Lead-Time Risk",
        "drivers_section": "Spend & Category Drivers",
    },
    "generic": {
        "risk_section": "Risk Assessment",
        "drivers_section": "Operational Drivers",
    },
}
```

The 6-section STRUCTURE is fixed (Exec Summary / Primary Metric / Segment
Decomposition / Drivers & Anomalies / Risk / Recommendations). Only the
LABELS for sections 4 and 5 adapt by domain.

## 8. Facet Executor — Hybrid Execution

Each facet spec becomes a `FacetResult`:

```python
{
    "facet_id": "regional_sales",
    "kind": "service_call" | "ad_hoc_query",
    "rows": list[dict],
    "summary": str,
    "source_sql": str,            # the executed SQL (for lineage appendix)
    "source_label": str,          # "erp_v_sale_orderentry" / "perception_service"
    "row_count": int,
    "warnings": list[str],        # ["50% of volume records null", ...]
    "available": bool,
    "unavailable_reason": str,    # populated when available=False
    "execution_log": [
        {"step": "service_invoke", "latency_ms": 1240, "status": "ok"},
        {"step": "row_validation", "latency_ms": 12, "status": "ok"},
    ],
}
```

- **service_call facets**: invoked directly via reflection
  (`getattr` walks `app.services.erp.kpi.ErpKpiService`,
  `app.services.perception.service.PerceptionService`). Whitelist of
  allowed services enforced in code (NOT user-supplied).
- **ad_hoc_query facets**: invoked via the existing
  `ask_data_agent` flow with `natural_language` + `suggested_tables`.
  This reuses the Two-Phase NL2SQL (Phase A: LLM picks 1-4 tables from
  the TOC; Phase B: selected tables get full DDL → SQL gen).
- **Per-facet timeout**: 60s. On timeout, the facet is marked
  `available=False, unavailable_reason="execution timed out after 60s"`.
- **Per-facet row validation**: a facet returning 0 rows or 1 row of
  generic aggregations (the degenerate pattern from the bug) is flagged
  `available=False, unavailable_reason="degenerate result"`. The
  synthesizer renders that section as "(data unavailable)".

## 9. Synthesizer + Claim Tracker — Truth-Backed

### 9.1 Deterministic transforms (NO LLM call)

- Rank rows by primary metric (revenue / volume / margin / etc.) —
  descending for positive metrics, ascending for cost/risk metrics.
- Compute shares (% of total), QoQ deltas (when prior period data is
  available in the same facet), concentration (top-3 share).
- Build the executive summary by extracting the headline number from
  the primary facet + the top anomaly from the drivers facet.
- Build recommended_actions from the rules table:

```python
RECOMMENDED_ACTION_RULES = [
    {
        "trigger": lambda facets: any(
            row.get("days_of_stock", 999) < 5
            for row in facets.get("inventory", {}).get("rows", [])
        ),
        "priority": "CRITICAL",
        "action_template": "Restock {product} immediately — only {days} days of stock",
        "source_facets": ["inventory"],
    },
    {
        "trigger": lambda facets: _top3_share(facets.get("sales", {})) > 0.60,
        "priority": "HIGH",
        "action_template": "Diversify customer base — top-3 concentration at {share}%",
        "source_facets": ["sales", "customer"],
    },
    {
        "trigger": lambda facets: _margin_compression_detected(facets),
        "priority": "HIGH",
        "action_template": "Review pricing — gross margin compressed by {delta}% in {segment}",
        "source_facets": ["sales", "cogs"],
    },
    # ... rules for inventory turn, forecast bias, attrition, etc.
]
```

### 9.2 Claim extraction

Each narrative sentence that cites a number becomes a `Claim`:

```python
{
    "claim_id": "c1",
    "text": "Revenue fell 18.0% QoQ to ¥301.9M",
    "source_facet": "regional_sales",
    "source_row_ids": ["row_3", "row_7"],   # rows in that facet
    "source_sql": "SELECT SUM(...) FROM erp_v_sale_orderentry WHERE ...",
    "verified": True,                       # set by ClaimTracker
}
```

The synthesizer emits claims ONLY for numbers it computed from the
facet rows. Free-text narrative ("This warrants attention") is allowed
without a claim citation; numeric assertions always get one.

### 9.3 Claim verification

`ClaimTracker.verify_claims(report) -> VerificationResult`:

- For each claim, re-execute the source SQL (cheap — same connection,
  cached plan) and confirm the cited rows still exist with the cited
  values (within rounding tolerance).
- A claim that fails verification is marked `verified=False` and its
  text is rewritten to: "Data unavailable for this claim (source rows
  not consistent with current state)."
- The report's `data_confidence.verified_claims` and
  `data_confidence.unverified_claims` counts are populated.

### 9.4 EnterpriseReport payload

```python
{
    "enterprise_report_kind": "executive",
    "title": "Last 30 days Financial Performance Report",
    "period_label": "2026-07-25 to 2026-08-23",
    "source_label": "ERP Sales DB + Perception Service + ad-hoc queries",

    # Section 1 — deterministic, no LLM
    "executive_summary": "Revenue fell 18.0% QoQ to ¥301.9M while gross
      margin compressed by 230bps in the Southern region. Cost of goods
      in the region rose 4.2%, outpacing the 1.8% price increase — the
      primary driver.",

    # Section 2 — primary metric breakdown
    "primary_metric_breakdown": {
        "rows": [...],
        "observations": "...",
    },

    # Section 3 — segment decomposition (region/product/customer)
    "segment_decomposition": {
        "rows": [...],
        "observations": "...",
    },

    # Section 4 — operational drivers & anomalies
    "operational_drivers": {
        "rows": [...],
        "anomalies": [...],
    },

    # Section 5 — domain-specific risk section (label from §7)
    "risk_section": {
        "label": "Cash-Flow Exposure",
        "rows": [...],
        "observations": "...",
    },

    # Section 6 — recommended actions (from rules table)
    "recommended_actions": [
        {"priority": "CRITICAL", "action": "...",
         "rationale": "...", "source": "..."},
        ...
    ],

    # Claim lineage
    "claims": [<Claim>, ...],

    # Data confidence
    "data_confidence": {
        "covered_facets": ["regional_sales", "regional_cogs", "discounting",
                           "competitor_benchmark", "regional_inventory"],
        "missing_facets": [{"facet_id": "competitor_benchmark",
                             "reason": "no competitor data table in catalog"}],
        "caveats": ["50% of volume records null in regional_sales facet"],
        "verified_claims_count": 12,
        "unverified_claims_count": 0,
    },
}
```

## 10. Dual Renderer — DOCX + HTML

### 10.1 DOCX renderer

- Cover page: title + period + source label
- Page break
- Section 1 (Executive Summary): H1 + paragraph + claim citations as
  superscript `[1]`, `[2]`
- Section 2 (Primary Metric Breakdown): H1 + table (rows from
  `primary_metric_breakdown.rows`) + observations paragraph
- Section 3 (Segment Decomposition): H1 + table + observations
- Section 4 (Operational Drivers & Anomalies): H1 + table + anomalies list
- Section 5 (Risk Section — domain-specific label): H1 + table + observations
- Section 6 (Recommended Actions): H1 + numbered list, each action with
  priority badge + rationale + source label
- Appendix (Data Lineage): H1 + per-facet subsection, each containing
  the source SQL in monospace, execution log table, row count

### 10.2 HTML chat renderer

- Inline markdown for the chat bubble — same 6 sections, same claim
  citations
- The lineage appendix is rendered as a collapsed `<details>` element
  at the end of the bubble (default collapsed; user can expand to see
  the SQL)
- Claim citations `[1]` are anchor links to the corresponding entry in
  the collapsed details block

Both renderers consume the SAME `EnterpriseReport` payload — no drift.

## 11. Error Handling & Fallback

1. **Profiler LLM call fails / times out** → return `None`; caller falls
   through to existing single-query path. Zero regression for any
   business query.
2. **A facet collector fails** → that facet becomes
   `{available: False, unavailable_reason: str(exc)[:200]}`. The other
   facets still complete. The DOCX renders the missing section as
   "(Data unavailable: {reason})".
3. **ALL facets fail** → the tool returns an error payload; the v3
   loop's existing fallback fires (`_data_rows_fallback`). No crash.
4. **Claim verification fails** → that claim's text is rewritten to
   "Data unavailable for this claim (source rows not consistent)". The
   report is still delivered; the user sees the gap explicitly.
5. **Title fallback** — `_title_from_prose` is hardened with
   `_GENERIC_HEADING_BLOCKLIST` = {"executive summary", "key metrics",
   "breakdown by dimension", "anomalies & risks", "recommended actions",
   "appendix note", "operational drivers", "primary metric breakdown",
   "segment decomposition"}. Returns `fallback` instead of heading text.
6. **DOCX renderer error** → falls back to existing `docx_export`
   default path. The degenerate 1-row case is blocked at the fusion
   layer by `_validate_enterprise_payload` (rejects any primary facet
   with `len(rows) < 2`).
7. **Profiler emits > 6 facets** → clamp to first 6.
8. **Profiler emits `kind=service_call` for non-whitelisted service** →
   that facet becomes `available=False, unavailable_reason="service not
   whitelisted"`. The whitelist is enforced in code, not by the LLM.

## 12. Testing Strategy

### Unit tests (deterministic, no LLM, no DB)
- `test_profiler.py` (~15): LLM mocked; intent classification, facet
  planning, JSON repair on malformed output, fail-open on timeout, 6+
  facets clamped.
- `test_executor.py` (~10): parallel fan-out, partial failure isolation,
  execution log populated, per-facet timeout, degenerate-row detection.
- `test_synthesizer.py` (~25): primary metric ranking, share computation,
  QoQ delta, recommended-actions rules (low-stock, concentration,
  margin-compression), missing-facet sections, payload validation
  rejecting < 2 rows.
- `test_claim_tracker.py` (~10): ungrounded claim rejected, source
  mapping round-trips, verified vs unverified counts, claim rewriting
  on verification failure.
- `test_docx_renderer.py` (~8): all 6 sections present, lineage appendix
  has SQL + execution log, NO "Executive Summary" as file title,
  claim citations rendered as superscripts, missing-facet section text.
- `test_html_renderer.py` (~7): inline markdown structure, collapsed
  `<details>` for lineage, citation anchors link to details entries.
- `test_domain_labels.py` (~5): all 8 domains map to labels; unknown
  domain falls back to generic.

### Integration tests (with v3 loop mocked)
- `test_enterprise_data_tool.py` (~5): tool invocation, payload structure,
  error propagation to v3 loop, regression — non-business query still
  takes the old path.

### Regression tests (existing — must still pass)
- `tests/services/artifacts/test_docx_no_toc.py` — TOC still removed
- `tests/services/generation/test_meaningful_title.py` — title generation
  still strips wrappers
- `tests/services/agent_prompts/test_metric_extraction.py` — metric
  keywords still detected
- `tests/services/generation/test_mine_data_rows.py` — historical reuse
- `tests/routers/test_normalize_tool_result.py`

Total: ~85 new tests + 5 regression suites.

## 13. System Prompt Block (insert into agent_prompts.py)

```
### ENTERPRISE BUSINESS-DATA REPORT PROTOCOL

When the user requests business data, performance metrics, operational
analysis, or executive insights:

1. INTENT & RESOURCE PROFILING:
   - Identify the primary business domain (Financial Performance, Supply
     Chain & Logistics, Sales Operations, Risk Management, HR, Procurement).
   - Inspect the available database schema and identify all relevant
     entities (transactions, inventories, customer records, market
     conditions).
   - Formulate a dynamic collection strategy requiring 3 to 6 distinct
     data facets necessary for a comprehensive executive report.

2. MULTI-SOURCE FACET EXECUTION:
   - Gather data across all identified facets in parallel.
   - If a specific facet or table is unavailable, document the data gap
     explicitly; DO NOT omit the remaining analysis.

3. DYNAMIC EXECUTIVE SYNTHESIS:
   - Synthesize raw data across all facets into a 6-section structure
     tailored to the user's specific context:
     * Section 1: Executive Summary & Strategic Takeaways
     * Section 2: Core Metric Analysis & Performance Breakdown
     * Section 3: Segment/Regional/Product Decomposition
     * Section 4: Operational Drivers & Anomalies
     * Section 5: Strategic Risk & Exposure Assessment
     * Section 6: Actionable Recommendations & Next Steps

4. ENTERPRISE RIGOR & TRUTH-BACKED CONSTRAINTS:
   - Every claim in the narrative MUST be grounded in the collected
     multi-source data.
   - Calculate percentage shifts, volume changes, and ratios explicitly —
     never provide empty or 1-row placeholder summaries.
   - Deliver the report simultaneously as a full inline Markdown response
     and a downloadable corporate DOCX document.
```

## 14. Deployment Plan

1. Code changes only — no new pip packages (reuses `python-docx`,
   `httpx`, `asyncio`, `re`, `json`).
2. No new flags required (the orchestrator is unconditional; non-matching
   queries fall through transparently).
3. No new migrations (no DB schema changes).
4. Restart: `docker restart zhanlu-backend zhanlu-sandbox-worker`.
5. Live verify: re-ask "Why are gross margins dropping in the Southern
   region despite rising sales?" and "Give me supply chain data for
   last 30 days" — expect 6-section DOCX + 6-section inline chat, domain-
   adaptive section labels, lineage appendix, title
   "{period} {domain_label} Report".

## 15. Performance & Latency Budget

| Step | Expected latency (deepseek-chat) | Mitigation |
|---|---|---|
| Profiler LLM call | 10-20s | Single call, structured JSON, no tools, temp=0, max_tokens=1500, timeout=30s |
| Facet execution (parallel) | 5-60s | asyncio.gather; per-facet timeout 60s; service_call facets ~1-2s each; ad_hoc facets 30-60s each (NL2SQL) |
| Synthesizer (deterministic) | <1s | No LLM call — pure Python transforms |
| Claim verification | 1-3s | Cached SQL re-execution on same connection |
| DOCX + HTML render | <1s | python-docx is in-memory |
| **Total** | **15-85s** | Comparable to current single-query deepseek-chat turn (30-90s) |

If latency becomes a problem in practice, Phase 2 can add a `ENTERPRISE_PROFILER_FAST_MODE` flag that bypasses the LLM planner for known-intent queries (supply-chain/sales/inventory) and uses a deterministic short-circuit — but that's an optimization, not a default.

## 16. Out of Scope (Phase 2 follow-up)

- LLM narrative polish layer on top of the deterministic summary
  (LLM-written 2-3 paragraph executive summary using the existing
  CEO-GRADE prompt — for now, the deterministic summary is the source
  of truth, which is safer for high-stakes decisions)
- Multi-period comparison (current vs prior period) with deterministic
  delta computation across multiple periods
- Optional: PDF + PPTX enterprise renderers reusing the same payload
- `ENTERPRISE_PROFILER_FAST_MODE` flag for known intents
- Streaming facet execution (start rendering as facets complete, rather
  than waiting for all to finish)

## 17. Self-Review

- [x] No placeholders; every section references concrete files / line
      numbers / existing services
- [x] Internal consistency: 3-6 facets → 6 sections (segments fold into
      Section 3; operational drivers + anomalies combine in Section 4;
      domain-specific risk in Section 5; recommendations in Section 6)
- [x] Scope: full Dynamic Enterprise Orchestrator (no phasing); Phase 2
      follow-ups explicitly deferred
- [x] No ambiguity: profiler LLM I/O specified, facet shapes specified,
      claim schema explicit, fallback paths documented
- [x] Backward compatible: non-business queries take the existing path
      (profiler returns None → caller falls through)
- [x] All flags / migrations: none required
- [x] Testing: deterministic tests for every layer; 5 regression suites
      must remain green
- [x] Truth-backed constraint: ClaimTracker with explicit verification
      step + ungrounded claim rewriting
- [x] Domain adaptivity: deterministic label table (NOT LLM-chosen) —
      section structure stable, labels adapt
- [x] Latency budget documented with mitigation per step
