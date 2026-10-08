# Phase 1 — Schema Catalog + Schema Linker: Implementation Plan

> **Parent spec**: `docs/superpowers/specs/2026-08-10-project-knowledge-graph-design.md`
> **Scope**: catalog indexer, schema linker, NLAnswerService + db_tools integration, flags, tests.
> **Out of scope**: document summarizer (Phase 2), entity extractor (Phase 3), "Data Map" UI tab (Phase 2), overlay REST API (Phase 2).
> **Next plan**: `docs/superpowers/plans/2026-08-10-phase2-docs-overlay.md` (after Phase 1 is done + user-validated).

---

## Task list (bite-sized, dependency-ordered)

### 1 — Data model: catalog tables + KnowledgeBase lifecycle field

**Files**:
- **NEW** `backend/app/models/knowledge_catalog.py`
- **MODIFY** `backend/app/models/knowledge_base.py`

**What**:
Create three SQLAlchemy models in `Base.metadata` (no alembic migration — follow the metadata/`create_all` convention of migrations 036/038; alembic head is `040_report_session_project_agent`):

- `KBTableMeta`: `id (String(32) PK)`, `kb_id (String(36) FK→KnowledgeBase)`, `schema_name (String(128))`, `table_name (String(256))`, `table_type (String(32), "TABLE"/"VIEW"/"MATERIALIZED_VIEW")`, `row_count (BigInteger, nullable)`, `size_bytes (BigInteger, nullable)`, `description_zh (Text, nullable)`, `description_en (Text, nullable)`, `sample_values (JSONB, nullable, first 3 rows)`, `indexed_at (DateTime, server_default=func.now())`. Unique: `(kb_id, schema_name, table_name)`.
- `KBColumnMeta`: `id (String(32) PK)`, `table_meta_id (String(32) FK→KBTableMeta, ondelete=CASCADE)`, `column_name (String(256))`, `ordinal (Integer)`, `data_type (String(64))`, `is_nullable (Boolean)`, `is_primary_key (Boolean, default=False)`, `default_value (Text, nullable)`, `description_zh (Text, nullable)`, `description_en (Text, nullable)`, `sample_values (JSONB, nullable)`. Unique: `(table_meta_id, column_name)`.
- `KBTableRelation`: `id (String(32) PK)`, `kb_id (String(36) FK→KnowledgeBase)`, `source_table_meta_id (String(32) FK→KBTableMeta)`, `target_table_meta_id (String(32) FK→KBTableMeta)`, `relation_type (String(16), "FK"/"INFERRED")`, `source_columns (JSONB, [])`, `target_columns (JSONB, [])`, `source_cardinality (String(16), "ONE"/"MANY", nullable)`, `target_cardinality (String(16), "ONE"/"MANY", nullable)`, `confidence (Float, default=1.0, only for INFERRED)`, `description (Text, nullable)`. Unique: `(source_table_meta_id, target_table_meta_id, source_columns, target_columns)`.

Add to `KnowledgeBase` (in `knowledge_base.py`):
```python
catalog_status = Column(String(16), default="pending", nullable=False, index=True)
# Lifecycle: pending → indexing → ready / error
# mirrors indexing_status pattern
```

**Acceptance**: models import cleanly; `Base.metadata.create_all()` creates the three tables + the new column on `knowledge_bases`; no DB errors.

**Depends on**: nothing (pure model creation).

---

### 2 — Config flags

**Files**:
- **MODIFY** `backend/app/config.py`

**What**:
Add two `Settings` fields near the existing `BI_*_ENABLED` block (around line 475–560):

```python
SEMANTIC_CATALOG_ENABLED: bool = False
SCHEMA_LINKING_ENABLED: bool = False
```

Both default `False`. Only Phase 1 needs these two (`ENTITY_GRAPH_ENABLED` deferred to Phase 3).

**Acceptance**: `SEMANTIC_CATALOG_ENABLED=true` in `.env` → `settings.SEMANTIC_CATALOG_ENABLED` is `True` after `docker compose up -d --force-recreate backend`.

**Depends on**: nothing.

---

### 3 — FK extraction for MySQL (+ Postgres readiness)

**Files**:
- **MODIFY** `backend/app/services/db/mysql.py` (or connector adapter layer — verify connector interface)
- **Possibly NEW** a shared helper in `backend/app/services/db/connectors/`

**What**:
Add a method to the connector that the catalog indexer calls during introspection:

```python
def get_foreign_keys(self, table_name: str, schema_name: str = None) -> list[dict]:
    """Return FK constraints for a table.
    Returns: [{"column": "product_id", "ref_table": "products", "ref_column": "id"}, ...]
    """
```

**MySQL implementation**:
```sql
SELECT kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_SCHEMA, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE kcu
WHERE kcu.TABLE_SCHEMA = DATABASE()
  AND kcu.TABLE_NAME = %s
  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
```

**Postgres readiness**: add `pg_catalog` or `information_schema` equivalent (stub or full impl) so `kb_table_relation` works for Postgres KBs too. If connector abstraction already handles this, just add the method; if not, add the MySQL one and a Postgres one.

**Acceptance**: `get_foreign_keys("erp_t_stk_inventory")` returns correct FK rows for the `demo_warehouse_prod` warehouse. Runs inside connector class without breaking existing methods.

**Depends on**: nothing (but catalog indexer will call it, so do this before or alongside Task 4).

---

### 4 — Catalog indexer

**Files**:
- **NEW** `backend/app/services/knowledge_graph/catalog_indexer.py`
- **NEW** `backend/app/services/knowledge_graph/__init__.py` (if not present; dir already has `models.py` for ACME BI product graph — these are separate files, same package)

**What**:
Central function:
```python
async def index_kb_catalog(kb: KnowledgeBase, db: Session) -> None:
```

Steps:
1. Set `kb.catalog_status = "indexing"` → `db.commit()`
2. Introspect tables via connector (`list_tables` + `describe_table` per table)
3. Extract FKs via `connector.get_foreign_keys()`
4. Fetch row counts: `SELECT COUNT(*) FROM {table}` (timeout 5s, cap — if timeout/error, store NULL)
5. **LLM description pass**: batch 5-8 tables at a time, one cheap model call per batch. Prompt: "Describe what this table contains and what each column means in business terms. The database is for [kb.description]. Output in JSON: `[{table, description_zh, description_en, columns: [{name, description_zh, description_en}]}]`". Use `settings.SEMANTIC_CATALOG_LLM_MODEL` (default `gpt-4o-mini` or `deepseek-chat` — make it configurable).
6. Persist to `kb_table_meta`, `kb_column_meta`, `kb_table_relation` (UPSERT pattern — `merge` or `on_conflict_do_update` so re-indexing is idempotent)
7. Embed into Chroma:
   - Collection name: `catalog_{kb_id}`
   - Per table: one document with text = `[table_name]\n{description_zh}\nColumns: {col1(name,type,desc), col2, ...}`. Metadata: `{"kb_id", "table_meta_id", "table_name", "row_count"}`
   - Per column: one document with text = `{table_name}.{column_name} [{data_type}]: {description_zh}`. Metadata: `{"kb_id", "table_meta_id", "column_name", "table_name", "is_pk"}`
   - Use `_get_client()` from `document_ingestion/store.py` (same PersistentClient singleton). Use `_NoopEmbeddingFunction` — call the embedding API ourselves and pass `embeddings=` to `.add()`. (Or use `settings.EMBEDDING_FUNCTION` for consistency.)
   - If collection exists, **delete + recreate** on re-index (ensures clean state). Use `client.delete_collection("catalog_{kb_id}")` then `get_or_create_collection`.
8. Set `kb.catalog_status = "ready"` → `db.commit()`
9. On any exception: set `kb.catalog_status = "error"` → `db.commit()`, log full traceback.

**Trigger**: called from a background task. Hook into the existing KB re-index flow or create a simple background runner:
- On `KnowledgeBase` PATCH where `indexing_status` changes → if `SEMANTIC_CATALOG_ENABLED` is true and `db_type` in (`mysql`, `postgres`), spawn `catalog_indexer.index_kb_catalog(kb, db)` via `asyncio.create_task` or existing background worker pattern.
- Also expose a manual trigger: `POST /api/knowledge-bases/{kb_id}/catalog/reindex` (simple — just spawns the task, returns 202).

**Acceptance**:
- Run `index_kb_catalog` against `demo_warehouse_prod` KB → `kb_table_meta` has 139 rows, each with descriptions; `kb_column_meta` has all columns; `kb_table_relation` has FKs.
- Check Chroma: `catalog_{kb_id}` collection exists with 2 × 139 ≈ 278 documents (tables + columns).
- `kb.catalog_status == "ready"`.
- Re-run is idempotent (UPSERT works, old Chroma collection deleted beforehand).
- Error case: `kb.catalog_status == "error"` after a simulated failure (e.g., connector down).

**Depends on**: Tasks 1, 2, 3 (models, flags, FK extraction).

---

### 5 — Schema linker

**Files**:
- **NEW** `backend/app/services/knowledge_graph/schema_linker.py`

**What**:
Core function:
```python
async def link_schema(
    question: str,
    kb_ids: list[str],
    top_k: int = 8,
    expand_joins: int = 2,   # hops
    token_budget: int = 800,
) -> dict:
    """
    Returns:
        {
            "tables": [{"name", "meta_id", "description_zh", "columns": [...], "rrf_score"}],
            "join_paths": [{"from_table", "to_table", "on", "hops"}],
            "slice_text": str,   # ~800-token curated DDL slice for LLM
            "total_tables_in_catalog": int,
        }
    """
```

Algorithm:
1. For each `kb_id`, retrieve from `catalog_{kb_id}` Chroma collection:
   - **Dense**: `collection.query(query_texts=[question], n_results=top_k * 2)` — returns table-level docs.
   - **Sparse**: `collection.query(query_texts=[question + " 表 列 字段"], n_results=top_k * 2)` — second query with Chinese table/column boost words to help CJK lexical matching. (Reuse `dense_hits_from_query` pattern from `hybrid_retrieval.py`.)
   - **RRF**: call `reciprocal_rank_fusion([dense_ranked, sparse_ranked], k=60)` from `backend/app/services/rag/hybrid_retrieval.py`.
   - Take top `top_k` tables by RRF score.
2. Expand join paths:
   - For each retrieved table, query `kb_table_relation` for direct FK relations (1 hop).
   - For 2-hop: join `kb_table_relation` with itself.
   - Prioritize: FK > INFERRED; ONE-to-MANY > ONE-to-ONE.
3. Assemble the curated slice:
   - DDL-like text for each table: `CREATE TABLE {name} (\n  {col1} {type} {nullable} {pk} -- {desc_zh}\n  ...\n);`
   - Join relationships as comments: `-- {from_table}.{col} → {to_table}.{col} (FK)`
   - Truncate to `token_budget` (count tokens with tiktoken, prioritize higher-RRF-score tables).
4. Return the structured result including `slice_text`.

**Fallback**: if no catalog exists for a `kb_id` (no Chroma collection, or `kb.catalog_status != "ready"`), return `None` — caller falls back to `SchemaService.describe_all()`.

**Acceptance**:
- Query "最近乙烯价格走势" against cataloged warehouse → top tables include `md_market_price`, not unrelated tables. RRF scores are monotonic (better matches get higher scores).
- Join paths from `md_market_price` include linked dimension tables.
- `slice_text` ≤ ~800 tokens (verify with tiktoken).
- No catalog → returns `None` (graceful degradation).
- Unit-testable in isolation: mock Chroma collection + mock `kb_table_relation` query → verify RRF outputs and join expansion.

**Depends on**: Task 4 (catalog indexer — needs the Chroma collection populated).

---

### 6 — Integration: NLAnswerService

**Files**:
- **MODIFY** `backend/app/services/db/nl_answer_service.py`

**What**:
At `answer()` method, between the guard clauses and the current `describe_all` call (around line 59):

```python
# NEW: schema linker path (flag-gated)
schema_info = None
if settings.SCHEMA_LINKING_ENABLED and settings.SEMANTIC_CATALOG_ENABLED:
    try:
        linker_result = await link_schema(
            question=question,
            kb_ids=[kb_id],
            top_k=8,
        )
        if linker_result and linker_result.get("slice_text"):
            schema_info = linker_result["slice_text"]
            logger.debug("NLAnswer: using catalog slice (%d tables in context)",
                         len(linker_result.get("tables", [])))
    except Exception as e:
        logger.warning("NLAnswer: schema linker failed (falling back to describe_all): %s", e)
        schema_info = None

# EXISTING fallback
if schema_info is None:
    try:
        schema_info = schema_svc.describe_all(kb_id, max_tables=30)
    except Exception as e:
        ...
```

**Acceptance**:
- With both flags ON + catalog ready → `schema_info` comes from `link_schema` slice. LLM generates SQL from the curated DDL.
- With either flag OFF → falls through to existing `describe_all` path. No behavior change.
- With flags ON but no catalog (status != ready) → gracefully falls back. Logged at WARNING, not ERROR.
- Existing tests still pass (they test the `describe_all` fallback path — no catalog exists).

**Depends on**: Tasks 2, 5 (flags + schema linker).

---

### 7 — Integration: db_tools.py

**Files**:
- **MODIFY** `backend/app/services/tool_handlers/db_tools.py`

**What**:
In the `_describe_schema` handler (or equivalent "describe the database" tool), add the same flag-gated schema linker path so the Data Agent's `describe_schema` tool also benefits:

```python
async def _describe_schema(kb_id: str, question: str = "", ...):
    if settings.SCHEMA_LINKING_ENABLED and settings.SEMANTIC_CATALOG_ENABLED:
        try:
            result = await link_schema(question, [kb_id])
            if result and result.get("slice_text"):
                return result["slice_text"]
        except Exception:
            pass  # fall through
    # Existing path
    return schema_svc.describe_all(kb_id, max_tables=30)
```

Verify the exact location — search for `describe_schema` or `describe_all` in `db_tools.py`.

**Acceptance**: Same behavior as Task 6 but in the tool-handler path. Agent using `describe_schema` tool gets catalog slice when available.

**Depends on**: Tasks 2, 5, 6 (flags + linker + NLAnswerService — same integration pattern).

---

### 8 — Trigger: KB lifecycle hook

**Files**:
- **NEW** `backend/app/services/knowledge_graph/catalog_triggers.py`
- **MODIFY** `backend/app/routers/knowledge_bases.py` (or wherever KB create/update/PATCH re-index is handled)

**What**:
`catalog_triggers.py`:
```python
async def maybe_reindex_kb_catalog(kb: KnowledgeBase, db: Session):
    """Called after KB changes. Spawn re-index if catalog-enabled + db type supported."""
    if not settings.SEMANTIC_CATALOG_ENABLED:
        return
    if kb.db_type not in ("mysql", "postgres"):
        return
    # Fire-and-forget background task
    asyncio.create_task(_reindex_with_error_handling(kb, db))
```

Wire this into the existing KB re-index flow (look for where `indexing_status` is set to `"indexing"` — possibly in `knowledge_bases.py` router or a service. Add a call to `maybe_reindex_kb_catalog` at the same point.

Also add a manual trigger endpoint:
```python
@router.post("/{kb_id}/catalog/reindex")
async def reindex_catalog(kb_id: str, db: Session = Depends(get_db), ...):
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if not kb:
        raise HTTPException(404)
    await index_kb_catalog(kb, db)
    return {"status": "accepted", "catalog_status": kb.catalog_status}
```

**Acceptance**: PATCHing a MySQL KB's name or re-indexing it triggers catalog reindex in background. `POST /.../catalog/reindex` returns 202 and `catalog_status` eventually becomes `"ready"`.

**Depends on**: Tasks 4, 2 (indexer + flags).

---

### 9 — Unit tests

**Files**:
- **NEW** `backend/tests/test_knowledge_graph/test_catalog_indexer.py`
- **NEW** `backend/tests/test_knowledge_graph/test_schema_linker.py`
- **NEW** `backend/tests/test_knowledge_graph/__init__.py` (if needed)

**What**:
`test_catalog_indexer.py` (SQLite fixture, no Chroma):
- Test `_introspect_tables`: mock connector returns 3 tables → 3 `KBTableMeta` rows.
- Test `_generate_descriptions`: mock LLM → JSON parsed into descriptions.
- Test `_persist_catalog`: write 3 tables → re-run → idempotent (UPSERT, no duplicates).
- Test error path: mock connector raises → `catalog_status == "error"`.

`test_schema_linker.py` (no Chroma, mock collection):
- Test RRF: 2 ranked lists → fused output respects RRF k=60 formula.
- Test join expansion: mock `kb_table_relation` rows → 1-hop and 2-hop paths returned.
- Test token budget: 100 tables in mock → slice ≤ `token_budget`.
- Test no-catalog: mock missing collection → returns `None`.
- Test `reciprocal_rank_fusion` directly: import from `hybrid_retrieval.py`, verify known fusion cases.

Run via `backend/venv/bin/python -m pytest tests/test_knowledge_graph/ -v` (SSH RAM limited — run individually, no backgrounding).

**Acceptance**: All tests pass. No regression on existing test suites (`test_forecasting/`, `test_experience/`).

**Depends on**: Tasks 4, 5 (need the code to test).

---

### 10 — Integration test (warehouse)

**Files**:
- **NEW** `backend/tests/test_knowledge_graph/test_integration_warehouse.py`

**What**:
One integration test against the real `demo_warehouse_prod` MySQL warehouse (requires `Acme_MYSQL_URL` env var + docker backend running):

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_catalog_flow():
    # 1. Create a KB pointing to demo_warehouse_prod
    # 2. Run catalog_indexer.index_kb_catalog(kb, db)
    # 3. Assert kb_table_meta.count() >= 100 (warehouse has 139 tables)
    # 4. Assert kb_column_meta.count() > kb_table_meta.count()
    # 5. Assert kb_table_relation.count() > 0 (FKs exist)
    # 6. Assert kb.catalog_status == "ready"
    # 7. Query schema linker with "最近乙烯价格走势"
    # 8. Assert "md_market_price" in linker_result["tables"][0]["table_name"] (top match)
    # 9. Assert linker_result["slice_text"] is non-empty and contains DDL
```

Skip in CI (no warehouse access). Mark with `pytest.mark.skipif` when `Acme_MYSQL_URL` is absent.

**Acceptance**: Passes when run manually against the warehouse. Fails informatively (not crashes) when warehouse is down.

**Depends on**: Tasks 4, 5, 8 (indexer + linker + triggers — needs the full pipeline wired).

---

### 11 — Eval set + measurement

**Files**:
- **NEW** `backend/tests/test_knowledge_graph/eval_questions.py`
- **NEW** `backend/tests/test_knowledge_graph/test_eval_table_recall.py`

**What**:
`eval_questions.py`: 10-15 Chinese natural-language questions with ground-truth expected tables:

```python
EVAL_QUESTIONS: list[dict] = [
    {"question": "最近乙烯华东市场价是多少？", "expected_tables": ["md_market_price"]},
    {"question": "上个月进口库存变化趋势", "expected_tables": ["erp_t_stk_inventory", "erp_t_stk_instock"]},
    # ... 10-15 total, covering different domains in the warehouse
]
```

`test_eval_table_recall.py`:
- For each question, run `link_schema` → collect retrieved table names.
- Compute **recall@K** (K=5, K=10): how many expected tables appear in top-K results.
- Compute **MRR** (mean reciprocal rank) of the first expected table.
- Print summary table: question, expected, retrieved, recall@5, recall@10, MRR.
- Also run `describe_all` (old path) and compare: how many of the same expected tables appear in the raw DDL dump? This gives a "before vs after" delta.

Run manually, not in CI suite.

**Acceptance**: Eval script runs end-to-end. Recall@10 ≥ 0.7 for at least 80% of questions. MRR ≥ 0.5. Before/after delta is positive (catalog beats raw dump on at least 2/3 of questions).

**Depends on**: Task 4, 5 (need populated catalog).

---

### 12 — Confirm & close Phase 1

**What**:
- Run full existing test suite: `backend/venv/bin/python -m pytest tests/ --ignore=tests/test_intelligence/ --ignore=tests/test_document_ingestion -x` — verify no regressions.
- Verify docker restart is clean: `docker restart zhanlu-backend` → backend healthy at port 5002.
- Manual smoke test: set `SEMANTIC_CATALOG_ENABLED=true, SCHEMA_LINKING_ENABLED=true` in `.env`, `docker compose up -d --force-recreate backend`, trigger `POST /.../catalog/reindex`, wait for `ready`, ask a question via the Data Agent chat.
- Update `MEMORY.md` with Phase 1 status (completed, flags tested, eval results).

**Depends on**: All above tasks.

---

## File summary

| File | Action |
|---|---|
| `backend/app/models/knowledge_catalog.py` | **NEW** — 3 SQLA models |
| `backend/app/models/knowledge_base.py` | **MODIFY** — add `catalog_status` |
| `backend/app/config.py` | **MODIFY** — 2 flags |
| `backend/app/services/db/mysql.py` | **MODIFY** — `get_foreign_keys()` |
| `backend/app/services/knowledge_graph/catalog_indexer.py` | **NEW** |
| `backend/app/services/knowledge_graph/schema_linker.py` | **NEW** |
| `backend/app/services/knowledge_graph/catalog_triggers.py` | **NEW** |
| `backend/app/services/db/nl_answer_service.py` | **MODIFY** — integration |
| `backend/app/services/tool_handlers/db_tools.py` | **MODIFY** — integration |
| `backend/app/routers/knowledge_bases.py` | **MODIFY** — reindex hook + `POST /catalog/reindex` |
| `backend/tests/test_knowledge_graph/__init__.py` | **NEW** |
| `backend/tests/test_knowledge_graph/test_catalog_indexer.py` | **NEW** |
| `backend/tests/test_knowledge_graph/test_schema_linker.py` | **NEW** |
| `backend/tests/test_knowledge_graph/test_integration_warehouse.py` | **NEW** |
| `backend/tests/test_knowledge_graph/eval_questions.py` | **NEW** |
| `backend/tests/test_knowledge_graph/test_eval_table_recall.py` | **NEW** |

---

## Execution order (dependency graph)

```
Task 1  (models)         ──┐
Task 2  (flags)          ──┤
Task 3  (FK extraction)  ──┤
                           ├──→ Task 4 (indexer) ──→ Task 8 (triggers)
                           │         │
                           │         └──→ Task 5 (linker)
                           │                  │
                           │         ┌────────┘
                           │         ▼
                           └──→ Task 6 (NLAnswer integration)
                                     │
                                     └──→ Task 7 (db_tools integration)
                                               │
                                               ▼
                                        Task 9 (unit tests)
                                               │
                                               ▼
                                        Task 10 (integration test)
                                               │
                                               ▼
                                        Task 11 (eval)
                                               │
                                               ▼
                                        Task 12 (confirm + close)
```

Tasks 1-3 are parallel-safe (different files, no shared state). Task 4 depends on 1+2+3. Task 5 depends on 4. Tasks 6-7 depend on 2+5. Tasks 9-11 depend on 4+5. Task 8 depends on 4.

---

## Risks specific to implementation

1. **LLM description cost**: ~139 tables × 1 batch = ~18 batches × ~3K tokens/batch = ~54K output tokens. With `gpt-4o-mini` (~$0.60/1M out) → ~$0.03 total. Negligible but worth noting that re-indexing too frequently costs tokens. Mitigation: only re-index on explicit trigger + on KB schema change (rare).
2. **Chroma collection naming collision**: `catalog_{kb_id}` is per-KB, safe. But need to ensure collection is deleted-and-recreated on re-index to avoid stale docs. The `delete_collection` + `get_or_create_collection` pattern is safe.
3. **FK extraction on views**: MySQL views don't have FKs — `get_foreign_keys` will return `[]`. That's correct behavior; the linker will skip join expansion for views.
4. **`asyncio.create_task` in FastAPI**: if the request lifecycle ends before the task, the session may be invalid. Need to create a new session inside the background task. Use `db_session_factory()` or `with get_db_context() as db:` pattern. Verify this pattern exists in the codebase.
5. **SQLite fixture for unit tests**: `kb_table_meta` etc. use JSONB — SQLite might need special handling (store as TEXT, serialize/deserialize). Use `TypeDecorator` or `sqlalchemy.dialects.postgresql.JSONB` with fallback. Check existing JSONB usage in the test fixtures — projects like `ForecastDecisionLog` may already have this pattern.
6. **Token counting**: need `tiktoken` in the backend venv. Verify it's already installed (likely is — used by LLM pipeline). If not, add to requirements.

---

## When to start

1. User signs off on this plan ("Accept plan and start now" or similar).
2. Execute tasks in dependency order, marking each complete in the todo list.
3. After each file write, verify imports/compile: `python -c "import filepath"` or `docker restart zhanlu-backend`.
4. After Tasks 9-10, run tests to confirm correctness.
5. After Task 12, present summary + eval results + MEMORY update.
