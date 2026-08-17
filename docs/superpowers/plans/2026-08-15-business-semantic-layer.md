# Business Semantic Layer — Implementation Plan

> Spec: `docs/superpowers/specs/2026-08-15-business-semantic-layer-design.md`
> For agentic workers: implement task-by-task. Use TDD (failing test → implement → pass → commit).

## Global Constraints

- All 4 new flags default `False`. Zero behavior change when off.
- Prompts stay domain-free. All domain content lives in `project_metric` rows.
- Migration number `052` (051 reserved by CEO morning-digest plan).
- After router edits: verify import (`docker exec zhanlu-backend python -c "from app.routers.project_catalog import router; print([r.path for r in router.routes])"`), then restart backend.
- Frontend build: `npx vite build --emptyOutDir=false` then `docker restart zhanlu-frontend`.
- Run pytest on specific file paths only (limited RAM, no parallel workers).

## File Map

### Modify
- `backend/app/models/knowledge_catalog.py` — add `ProjectMetric` model + `KBTableMeta.coverage_json`
- `backend/app/config.py` — 4 flags
- `backend/app/services/knowledge_graph/catalog_indexer.py` — coverage probe
- `backend/app/services/knowledge_graph/schema_linker.py` — coverage annotation
- `backend/app/services/db/nl_answer_service.py` — freshness short-circuit + context injection + narration rewrite
- `backend/app/services/tool_handlers/db_tools.py` — validation hints
- `backend/app/routers/agents.py` — artifact-aware fallback
- `backend/app/routers/project_catalog.py` — metrics endpoints
- `frontend/src/api/projectCatalog.js` — listMetrics/putMetric/bootstrapMetrics
- `frontend/src/components/data-map/DataMapSection.jsx` — Metrics tab
- `frontend/src/lib/translations.js` — dataMap.metrics.* keys

### Create
- `backend/app/services/knowledge_graph/business_context.py`
- `backend/app/services/knowledge_graph/metric_bootstrap.py`
- `backend/alembic/versions/052_business_semantic_layer.py`
- `backend/tests/test_business_context.py`
- `backend/tests/test_coverage_probe.py`
- `backend/tests/test_executive_narration.py`
- `backend/tests/test_artifact_fallback.py`
- `backend/tests/test_metric_bootstrap.py`
- `backend/tests/test_project_catalog_metrics.py`

---

## Task 1: Spec doc

- [x] Write `docs/superpowers/specs/2026-08-15-business-semantic-layer-design.md`

## Task 2: Model + migration + flags

- [ ] Add `ProjectMetric` model to `knowledge_catalog.py` (inherit `TimestampedBase`, JSON columns normalized with `coerceStringArray` on aliases/bindings).
- [ ] Add `coverage_json = Column(JSON)` to `KBTableMeta`.
- [ ] Write migration `052_business_semantic_layer.py`: add column + create table.
- [ ] Add 4 flags to `config.py` KG block.
- [ ] Verify import + run a smoke `pytest` on model import.

## Task 3: Bug fixes (Phase 1)

- [ ] `agents.py` artifact-aware fallback: in else-branch at ~8040, if `_orch_created` non-empty, build from artifact title+summary.
- [ ] Rewrite `_NARRATE_SYSTEM_PROMPT` in `nl_answer_service.py` to executive contract.
- [ ] Test: `test_artifact_fallback.py`, `test_executive_narration.py`.

## Task 4: Coverage probe + freshness short-circuit (Phase 2)

- [ ] `catalog_indexer.py`: in `_introspect_one_table`, probe `min_date`/`max_date` (5s timeout, mirror `_safe_row_count`) → `coverage_json`.
- [ ] `business_context.py`: `parse_relative_window`, `freshness_verdict`, `build_business_context`.
- [ ] `nl_answer_service.py`: short-circuit in `answer()` when `KG_FRESHNESS_SHORTCIRCUIT_ENABLED`.
- [ ] Test: `test_coverage_probe.py`, `test_business_context.py`.

## Task 5: Business context + bootstrap + hints (Phase 3)

- [ ] `metric_bootstrap.py`: LLM-propose `project_metric` rows (status `proposed`).
- [ ] `schema_linker.py`: append coverage annotation lines to `slice_text`.
- [ ] `nl_answer_service.py`: inject `build_business_context` output into `_SQL_GEN_SYSTEM_PROMPT`.
- [ ] `db_tools.py`: attach validation hints to `execute_query` result.
- [ ] Test: `test_metric_bootstrap.py`.

## Task 6: Metrics API + UI

- [ ] `project_catalog.py`: `GET /projects/{id}/catalog/metrics`, `PUT .../metrics/{metric_id}`, `POST .../metrics/bootstrap`.
- [ ] `projectCatalog.js`: `listMetrics`, `putMetric`, `bootstrapMetrics`.
- [ ] `DataMapSection.jsx`: Metrics tab (list/approve/edit/bootstrap).
- [ ] `translations.js`: `dataMap.metrics.*` (en/zh).
- [ ] Test: `test_project_catalog_metrics.py`.

## Task 7: Verify + deploy

- [ ] Run all targeted pytest files.
- [ ] Verify router import; restart backend.
- [ ] `npx vite build --emptyOutDir=false`; restart frontend.
- [ ] Manual smoke: stale-window question → ~1s plain answer; report artifact → no generic fallback.
