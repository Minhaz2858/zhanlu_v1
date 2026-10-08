# Schema-Aware Multi-Table Analysis — Design

**Date:** 2026-08-17 · **Scope:** `acme_bi_assistant` first · **Status:** accepted

## Goal
Agent discovers DB structure at runtime and reasons about joins. Zero business-keyword
hardcoding. Only `information_schema` metadata, types, keys, sampled values, sample rows.
LLM does all semantic reasoning and narrates "extend the analysis" offers itself.

## Two-layer architecture (max reuse)
- **L1 retrieval:** `schema_linker.link_schema()` selects ~8 candidate tables (RRF/Chroma). Unchanged.
- **L2 presentation+edges:** new `SchemaGraph` over candidates only, reusing `SchemaService`
  cached introspection + `kb_table_relation` edges, rendered via `describe_schema`.
- New `schema_validator.py` (sqlglot) grounds every query against the real schema.

## Join edges (index time — value overlap too expensive live)
Ranked `FK (1.0) > VALUE_OVERLAP (containment score) > NAME_MATCH (0.5)`.
- **FK**: declared FKs, persisted by `_persist_relations`.
- **VALUE_OVERLAP**: containment of sampled distinct values, type-compatible, language-agnostic.
- **NAME_MATCH**: same name + compatible type, excluding noise stoplist.

**Noise stoplist (structural, not business):** `id, created_at, updated_at, status, name, type,
remark, created_by, is_deleted, org_id, tenant_id, state, version, updated_by, deleted`.

**Type buckets:** `int` and `varchar` only; float/date/blob/bool excluded.

**O(n²) avoidance:** value → columns inverted index; containment only for pairs sharing ≥ 3 values.

**Unique constraint:** one row per directed pair; inferred edges use canonical alphabetical
direction; pairs already covered by FK are skipped. **Ranking guard on upsert (§6).**

## Runtime SchemaGraph (`services/db/schema_graph.py`)
`TableNode` (columns type-only, `row_count_approx` from `information_schema.TABLES.TABLE_ROWS`
— never `COUNT(*)`, capped `sample_rows`, `edges`); `build()`, `get_related_tables()` ranked
FK > VALUE_OVERLAP > NAME_MATCH; `to_llm_context(focus_table, token_budget)` budgeted renderer.

## SQL validator (`services/nl2sql/schema_validator.py`)
sqlglot parse → every referenced table/column must exist (lazy per-table `describe_table`,
TTL-cached). Returns `{is_valid, errors, available_columns}`. Feedback, not gate: agent loop
uses the structured error as the retry mechanism (v3 FSM feeds errors back to the LLM);
`NLAnswerService` does one explicit retry.

## Join policy (prompt protocol, generic)
Graph context annotates every edge with kind+confidence. Protocol: query best table → check
edges → auto-join on FK or overlap ≥ `SCHEMA_GRAPH_AUTOJOIN_THRESHOLD` (0.8); ask only if
ambiguous; automation runs never ask; always cite table + join column; never claim "I only
have X table" without checking edges.

## Flags (all default False)
`SCHEMA_GRAPH_ENABLED`, `SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED`, `CATALOG_JOIN_EDGES_ENABLED`.
Tunables: `SCHEMA_GRAPH_AUTOJOIN_THRESHOLD=0.8`, `SCHEMA_GRAPH_SAMPLE_ROWS=3`,
`SCHEMA_GRAPH_TOKEN_BUDGET=1200`, `JOIN_EDGE_SAMPLE_LIMIT=200`.

## Safety / blast radius
Graph path sits inside `_describe_schema` after the `restricted` check — policy-restricted KBs
keep the filtered path. Dialect from `kb.db_type` (map `postgresql`→`postgres`). Logging: one
INFO per edge run; validator rejections WARNING with table/column names only. All paths
flag-gated off; `link_schema` returns None → `describe_all` fallback inherited. No frontend or
router changes.

## Eval
~20 questions across 3 synthetic schemas (ERP / SaaS / German names) + live MySQL warehouse
(`demo_warehouse_prod`). Metrics: hallucinated-column rate, join correctness vs gold,
tool iterations vs `describe_all` baseline.

## §6 Ranking guard (upsert, explicit user requirement)
When persisting an inferred edge to `kb_table_relation`:
1. If an existing edge for the same directed pair is `FK` → never downgrade (skip + WARNING).
2. Else overwrite only if new confidence is **strictly higher**; otherwise skip + WARNING.
