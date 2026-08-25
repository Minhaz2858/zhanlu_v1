# Ecisco BI Project Knowledge Cache — Design Spec

**Date:** 2026-08-25
**Status:** Approved
**Scope:** Only the `ecisco_bi_assistant` AgentApp; no impact on other projects.

## Goal

Pre-build and continuously maintain a per-project knowledge cache for the
`aipdp_data_warehouse_prod` database so that agents — especially Qwen models —
can answer Ecisco BI questions with fewer LLM round-trips. The cache combines:

1. The existing static C5/C9 product value-chain graph (already live).
2. The existing catalog indexer's table/column metadata (already live).
3. A new project-scoped facade (`ProjectKnowledgeCache`) that owns the write
   trigger and a layered query router used by the agent loop.

The win for Qwen: for any question that resolves to a product, entity, or
metric, the cache returns a structured answer **before** the agent's first
LLM call. For everything else, behaviour is unchanged.

## Non-goals

- This spec does **not** modify the static C5/C9 graph or its resolver.
- It does **not** introduce new DB tables.
- It does **not** turn on any behaviour by default; every new flag defaults False.
- It does **not** change SQL generation, NL2SQL, or the synthesis layer.
- It applies only to the Ecisco BI project; other AgentApps are untouched.

## Architecture

### Module layout (under `backend/app/services/project_knowledge/`)

```
project_knowledge/
├── __init__.py
├── cache.py              # ProjectKnowledgeCache — unified facade
├── ingestion.py          # Indexer + entity-link seeding
├── fast_path.py          # Qwen pre-LLM answer router
├── entity_linker.py      # C5/C9 ProductNode → catalog table mapping
├── router.py             # FastAPI router for admin endpoints
├── models.py             # CacheStatus, CacheStats, CacheQueryResult
└── tests/
    ├── test_cache_query.py
    ├── test_entity_linker.py
    ├── test_fast_path.py
    ├── test_integration_ecisco_cache.py
    └── test_scoped_to_ecisco_bi.py
```

### Public API (the cache facade)

```python
class ProjectKnowledgeCache:
    def __init__(self, project_id: str): ...
    def ingest(self, kb_id: str) -> CacheStatus: ...
    def query(self, question: str, *, model_id: str | None = None) -> CacheQueryResult | None: ...
    def invalidate(self, scope: Literal["all", "links", "metrics"] = "all") -> int: ...
    def stats(self) -> CacheStats: ...
```

All three operations are strictly project-scoped. Cross-project reads raise
`PermissionError`.

### Reused tables (no new schema)

- `project_entity` — for C5/C9 product nodes seeded as `concept|product` rows.
- `project_entity_link` — for `entity → table/column` rows.
- `project_catalog_overlay` — for curator-supplied alias overrides.
- `project_metric` — for cached business metrics.
- `kb_table_meta`, `kb_column_meta`, `kb_table_relation` — produced by the
  existing `catalog_indexer.index_kb_catalog` (unchanged).
- `resource_registry` — produced by the existing `registry_indexer` (unchanged).

### Configuration (all default False)

```python
# config.py additions (in the ECISCO_BI block, alongside 5A–5D)
ECISCO_BI_KG_PROJECT_CACHE_ENABLED: bool = False
ECISCO_BI_KG_QWEN_FAST_PATH: bool = False
QWEN_FAST_PATH_MODEL_PREFIXES: list[str] = ["qwen", "Qwen"]
ECISCO_BI_AGENT_NAMES: list[str] = ["ecisco_bi_assistant"]
PROJECT_KNOWLEDGE_INGEST_BG_WORKER: bool = True   # use asyncio.to_thread, not blocking
```

## Write path — Ingestion

### Trigger

- **On KB bind.** When `AgentApp.knowledge_bases` mutates and
  `agent.name in settings.ECISCO_BI_AGENT_NAMES`, queue
  `ingest_for_project(project_id, kb_id)` via `asyncio.to_thread`.
- **On KB update.** When a `KnowledgeBase` row's `db_schema_version`
  changes, re-run ingestion for any project bound to it.
- **Manual.** Admin endpoint `POST /api/project-knowledge/reindex`
  accepts `{project_id, kb_id}` and runs synchronously, returning the
  `CacheStatus` result.

### Pipeline

```python
async def ingest_for_project(project_id: str, kb_id: str) -> CacheStatus:
    # 1. Catalog — introspect + describe + persist + embed (idempotent)
    await index_kb_catalog(kb, db)                      # existing function

    # 2. Entity seed — promote the 12 C5/C9 products to project_entity rows
    seed_products_as_entities(db, project_id)           # new function

    # 3. Entity → catalog linking — product names ↔ md_t_lz_price-style tables
    link_entities_to_catalog_for_project(db, project_id, kb_id)  # new function

    # 4. Registry sync — push KB into ResourceRegistry (existing, flag-gated)
    index_knowledge_base(db, kb, project_id=project_id) # existing

    return CacheStatus(status="ready", tables=N, entities=N, links=N)
```

All four steps are idempotent and individually flag-gated. Failures in
steps 2–4 downgrade `CacheStatus` to `"partial"` but do **not** mark the
KB as `error`. Step 1 failures use the existing error path.

### Refreshing / invalidating

- `cache.invalidate("links")` deletes `project_entity_link` rows for the
  project; next query re-derives them lazily on demand.
- `cache.invalidate("all")` clears links + overlay + metric rows. Catalog
  tables are never auto-deleted — they're owned by the catalog indexer.
- Run schedule (optional, off by default): nightly re-link, weekly re-seed.

## Read path — Query routing

`cache.query(question, model_id=None) -> CacheQueryResult | None`

Layered lookup; **first non-None wins**.

```
question
  │
  ├─► Layer 1: Resolver
  │     resolve_product_id(question)  [in-memory; instant]
  │     hit → CacheQueryResult(kind="product", data={upstream|downstream|full})
  │
  ├─► Layer 2: Project entities
  │     load ProjectEntity by name/alias token match
  │     hit → CacheQueryResult(kind="entity", data={entity, links})
  │
  ├─► Layer 3: Project metrics
  │     load ProjectMetric by name/alias token match
  │     hit → CacheQueryResult(kind="metric", data={name, sql_expression, unit, ...})
  │
  └─► Layer None
        return None  → caller proceeds with normal LLM loop
```

Each layer is its own module function. Layers can be disabled independently
via their own flag (e.g. `ECISCO_BI_KG_LAYER_ENTITIES_ENABLED`,
`ECISCO_BI_KG_LAYER_METRICS_ENABLED`).

### Layer 1 (Resolver) detail

Reuses `app.services.knowledge_graph.resolver.resolve_product_id` unchanged.
Supports EN/CN aliases for all 12 C5/C9 products plus any custom aliases
added via `ProjectCatalogOverlay.scope="table_alias"`.

### Layer 2 (Entities) detail

- Loads `ProjectEntity` rows for `project_id` where `source="c5c9_seed"`
  OR `source="memory_extracted"`.
- Matches when the question contains any of `name` or `aliases` (case-insensitive
  substring; **CJK-safe** — uses `(?:^|[^\w])` lookarounds, never `\b` around CJK).
- Returns up to 3 entity hits with their `ProjectEntityLink` rows attached.

### Layer 3 (Metrics) detail

- Loads `ProjectMetric` rows for `project_id` where `status="approved"`.
- Same matching rules as Layer 2.
- Returns at most 1 hit (the highest-confidence one).

## Qwen fast-path injection

### Where it runs

In `backend/app/routers/agents.py`'s main chat FSM (the v3 streaming loop
around line 7000), **before** the first LLM call of a turn, inject:

```python
if settings.ECISCO_BI_KG_QWEN_FAST_PATH and is_qwen_model(model_id):
    cached = cache.query(user_message, model_id=model_id)
    if cached is not None:
        # Inject as a structured context block; force tool_choice="none"
        # for the first 1–2 iterations OR until the agent has consumed
        # the cached block in its response.
        _inject_cached_answer_into_context(messages, cached)
        _contract_force_synthesis = True   # existing flag
        _fast_path_consumed_at = iter
```

### What it skips

- The planning FSM step (`should_plan=True`) when `cached.kind in {"product", "entity", "metric"}` and
  the cached data already answers the question.
- The first schema-linker + NL2SQL round-trip.
- The first synthesis-LLM call (when data is in the cached block).

It does **not** skip:
- Per-iteration verification (we still need to check the agent actually used
  the cached answer).
- Post-loop fallback if the agent ignores the cached block (existing
  `data_rows_fallback` path remains in place).

### Failure modes

- Cache layer returns wrong product → agent still sees the cached block but
  has the LLM in the loop, so it can recover by calling `ask_data_agent`.
- Cache DB read errors → returns `None`; LLM loop runs normally.
- ChromaDB unavailable → resolver + entity + metric layers still work
  (they don't touch Chroma); only catalog-indexed fast-path features degrade.

## Project scoping & isolation

- Every `ProjectKnowledgeCache` instance takes `project_id`; queries join
  `WHERE project_id = :pid`.
- Cross-project reads throw `PermissionError` (caught at FastAPI layer as 403).
- Ingestion only fires for AgentApps whose `name` is in
  `settings.ECISCO_BI_AGENT_NAMES` (default: `["ecisco_bi_assistant"]`).
- The module flag `ECISCO_BI_KG_PROJECT_CACHE_ENABLED=False` globally gates
  every operation; turning it on for one tenant is opt-in per deployment,
  not per project.

## Error handling & degradation

| Failure | Behaviour |
|---|---|
| Ingestion step 1 (catalog) fails | Existing path; `kb.catalog_status="error"`; cache returns `None` |
| Ingestion step 2 (seed) fails | Cache returns `Status.partial`; layer 1 (resolver) still works |
| Ingestion step 3 (link) fails | Layer 2 disabled; resolver + metrics still work |
| Ingestion step 4 (registry) fails | Silent (existing behaviour) |
| Resolver miss on question | Layer 1 returns `None`; next layer tried |
| Chroma unavailable | Layers 1+2+3 unaffected; only embedding-based link queries degrade |
| LLM call (during seeding) fails | Fall back to rule-based linker (regex + aliases); lower confidence threshold 0.5 |
| Fast-path mis-classifies | LLM still runs; cached block is context, not the answer |

**Every layer fails open.** The LLM is always the safety net.

## Testing strategy

### Unit tests

- `test_cache_query.py` — Layer 1/2/3 routing order, miss behaviour, project scoping
- `test_entity_linker.py` — product → table mapping, alias coverage, CJK-safe regex
- `test_fast_path.py` — Qwen prefix detection, fast-path injection points,
  LLM-loop fallback when cache miss

### Integration

- `test_integration_ecisco_cache.py` — runs against `aipdp_data_warehouse_prod`
  when `EDIA_MYSQL_URL` is set; SQLite-fallback otherwise.
  - Ingest a fake KB; assert `kb.catalog_status=ready`, ≥100 tables catalogued.
  - Query a real product; assert `kind="product"`, non-empty data block.
  - Query a non-product question; assert `None` (fallback).

### Regression / safety

- `test_scoped_to_ecisco_bi.py` — creates 2 AgentApps, only the one named
  `ecisco_bi_assistant` triggers ingestion; the other is unaffected even
  when its KB has the same name.
- `test_fast_path_no_llm_when_hit.py` — mocks `call_llm`; with a cache hit,
  assert `call_llm` is called **fewer** times than without the fast path.
- `test_no_regression_for_other_agents.py` — existing
  `tests/test_knowledge_graph/*` all continue to pass with all flags False.

### Live verification (post-deploy)

1. Set `ECISCO_BI_KG_PROJECT_CACHE_ENABLED=True`,
   `ECISCO_BI_KG_QWEN_FAST_PATH=True` in `.env`.
2. Pick the `Ecisco BI` AgentApp, bind the warehouse KB, click "Reindex"
   in admin UI (or `POST /api/project-knowledge/reindex`).
3. Open chat as a user with `qwen3.6-27b` selected; ask "异戊二烯的上游有哪些?"
   → expect response within ≤2s with no `ask_data_agent` call (visible in
   the activity panel).
4. Ask "Give me the July sales report" → expect normal NL2SQL path,
   unchanged from today.
5. Compare pre/post metrics in `dashboard/admin/system` — total LLM calls
   per turn for the first question type should drop by ≥2.

## Open questions (deferred)

These were considered and **explicitly deferred** to follow-up specs:

1. **Auto-promotion of `ProjectMetric`** — `proposed → approved` is human-driven
   for now. Future: add a curator-side UI.
2. **Cross-KB entity linking** — when Ecisco BI binds multiple KBs (warehouse +
   CRM, say), should entity links merge? Future, when the need arises.
3. **Cache TTL** — current design treats the cache as "always fresh on ingest".
   Time-based TTL is a future concern.
4. **Per-tenant isolation** — `org_id` and `app_id` are part of every query;
   we currently don't hard-enforce tenant rejection. Deferred to a tenancy
   hardening pass.
