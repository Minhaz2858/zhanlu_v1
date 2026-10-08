# KG Comprehensive Implementation — Master Plan

**Date:** 2026-08-10
**Spec:** `docs/superpowers/specs/2026-08-10-project-knowledge-graph-design.md` (revised, 5 phases)
**Status:** In execution

## Execution order & gates

| # | Task | Flag (default OFF) | Gate / verification |
|---|---|---|---|
| 1 | Spec revision (5 phases, registry/recipes/depth sections, generic-entity constraint) | — | spec doc updated |
| 2 | Phase 1 eval gate: 15 questions, recall + latency, fix linker gaps | `SEMANTIC_CATALOG_ENABLED`, `SCHEMA_LINKING_ENABLED` | table recall ≥ 90%, latency recorded |
| 3 | nl2sql shadow wiring into `ask_data_agent` path + `validate_sql` facade | `NL2SQL_SHADOW_MODE_ENABLED` | divergence logs written; served result invariant |
| 4 | Resource router: `RouteDecision` contract + deterministic rules | `KG_RESOURCE_ROUTER_ENABLED` | route accuracy ≥ 90% on fixtures; never-raise |
| 5 | Resource registry: model + migration 048 + indexers + visibility + read API | `KG_RESOURCE_REGISTRY_ENABLED` | upsert idempotency; visibility filter test |
| 6 | Entity graph fold-in: tables + migration 049 + GENERIC extractor + entity-aware linker + Data Map prompt block | `ENTITY_GRAPH_ENABLED` | extraction precision ≥ 0.8 on neutral fixtures; no domain vocabulary |
| 7 | Report recipes: model + migration 050 + runner + 5 generic seeds + learning hooks | `REPORT_RECIPES_ENABLED` | end-to-end fixture run; sandbox soft-fail |
| 8 | Depth analysis loop: critic + evidence pack + sandbox stats + soft-fail | `DEPTH_ANALYSIS_LOOP_ENABLED` | iteration cap; evidence pack shape |
| 9 | Data Map UI tab: catalog browser + overlay edit + registry view | (reads 2/5/6 APIs) | vitest; renders per RBAC |
| 10 | Rollout: full tests, docker restart, rollout doc with recorded numbers | all | verified command output only |

## Hard rules

- Every new table/column ships with its alembic migration in the same task (048/049/050). Verify DB head first; clear stale `/app/alembic/__pycache__` in container.
- SQL → direct read-only QueryService; Python/charts → sandbox. Never reversed.
- Flags off = byte-identical current behavior; coded fallbacks preserved everywhere.
- `agents.py` chat FSM untouched. (If ever touched: initialize-before-conditional rule + AST regression test.)
- Entity enrichment fully generic: no domain-specific examples (no "sample product", no product/industry names) in prompts, seeds, or tests.
- Tests individually via `backend/venv/bin/python -m pytest <file>`; avoid `tests/test_intelligence/`, `tests/test_document_ingestion`.
- Ops: `docker restart zhanlu-backend` after service changes; `.env` flips need `docker compose up -d --force-recreate backend`.

## Key file map

- Router: `backend/app/services/knowledge_graph/resource_router.py` (new), `backend/app/services/intent_classifier.py` (extend taxonomy)
- Shadow: `backend/app/services/nl2sql/__init__.py` (facade), `backend/app/services/db/nl_answer_service.py`, `backend/app/services/tool_handlers/delegation_tools.py`
- Registry: `backend/app/models/resource_registry.py`, `backend/app/services/knowledge_graph/registry_indexer.py`, `backend/app/routers/project_catalog.py`
- Entities: `backend/app/models/knowledge_catalog.py` (+3 models), `backend/app/services/knowledge_graph/entity_extractor.py`, `schema_linker.py` (entity sub-query), `data_source_runtime.py` (Data Map block)
- Recipes: `backend/app/models/report_recipe.py`, `backend/app/services/report_recipes/{runner,seed_recipes}.py`
- Depth: `backend/app/services/depth_analysis/{loop,evidence_pack}.py`
- UI: project page "Data Map" tab + `frontend/src/components/data-map/`
