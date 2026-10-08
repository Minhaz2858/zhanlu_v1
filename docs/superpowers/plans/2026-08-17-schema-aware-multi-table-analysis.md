# Schema-Aware Multi-Table Analysis — Implementation Plan

> TDD per module. Run tests individually (SSH RAM). `docker restart zhanlu-backend` after
> backend changes. All flags default `False`.

## Phase 1 — Join edge inference (index time)

- [ ] **1.1** Failing test `backend/tests/test_join_edge_detector.py`: 3 fixtures (ERP /
      SaaS / German names) — overlap without FK, noise-column exclusion, type mismatch
      rejection, confidence ranking.
- [ ] **1.2** `services/knowledge_graph/join_edge_detector.py`: `detect_join_edges(tables)`
      → edge dicts (pure, no I/O). Value-overlap inverted index, name-match w/ stoplist,
      type buckets, FK-pair skip, canonical direction.
- [ ] **1.3** `config.py`: `CATALOG_JOIN_EDGES_ENABLED`, `JOIN_EDGE_SAMPLE_LIMIT=200`.
- [ ] **1.4** `catalog_indexer.py`: `_sample_joinable_values` (int+varchar, capped, skips
      already-sampled); `_persist_join_edges` with ranking guard; wire after
      `_persist_relations` gated by flag.
- [ ] **1.5** Green: `backend/venv/bin/python -m pytest tests/test_join_edge_detector.py -v`.
- [ ] **1.6** Commit.

## Phase 2 — SchemaGraph + validator + prompt protocol

- [ ] **2.1** `services/db/schema_graph.py` (TDD in `tests/test_schema_graph.py`).
- [ ] **2.2** `schema_linker._expand_joins` surfaces `kind` + `confidence`.
- [ ] **2.3** `services/nl2sql/schema_validator.py` (TDD in `tests/test_schema_validator.py`).
- [ ] **2.4** `db_tools.py`: `_describe_schema` graph fast path + `_execute_query` validator
      (behind `SCHEMA_GRAPH_ENABLED` / `SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED`);
      `nl_answer_service.py` graph context + one retry. Tests `test_describe_schema_graph.py`.
- [ ] **2.5** `agent_prompts.py`: generic Schema-Aware Query Protocol block at acme
      injection point; confirm automation-context signal.
- [ ] **2.6** Flags: `SCHEMA_GRAPH_ENABLED`, `SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED`,
      `SCHEMA_GRAPH_AUTOJOIN_THRESHOLD=0.8`, `SCHEMA_GRAPH_SAMPLE_ROWS=3`,
      `SCHEMA_GRAPH_TOKEN_BUDGET=1200`.
- [ ] **2.7** Commit.

## Phase 3 — Eval + rollout

- [ ] **3.1** `backend/scripts/eval_schema_graph.py`: 3 synthetic schemas + optional live
      warehouse; metrics (hallucination, join correctness, iterations) vs baseline.
- [ ] **3.2** Run eval, record numbers.
- [ ] **3.3** `docker restart zhanlu-backend`; verify flags default off; live smoke test.
- [ ] **3.4** Commit.
