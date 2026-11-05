# Stage 2A Design — Dynamic Database Schema Classification

Date: 2026-10-30
Status: DRAFT — awaiting approval before Stage 2B implementation
Parent: FSM persistence Stage 1 (implemented, uncommitted); wrong-table root-cause investigation (conv 7ba23bea, regional view selected 7× for company-wide questions)

## 0. Goal and acceptance criterion

A user connects an **unfamiliar** database, asks a business question in natural language, and receives an accurate answer using the correct tables, measures, relationships, and **data scope**.

Hard constraints:
- **Zero hardcoded table names, customer-specific rules, or domain vocab** (no "ERP", no "sales"). All classification is derived from structural signals, observed data, source comments, and human overrides.
- **Multi-engine**: PostgreSQL, MySQL, SQL Server, SQLite, Oracle (at least introspection parity), multi-tenant.
- **Never silently return a convincing report based on an incorrectly scoped dataset** — degrade loudly, refuse honestly, or clarify.

## 1. What already exists (reuse, do not rebuild)

Investigation (file:line anchors from the 2026-10-30 code-explorer pass):

| Dimension | Status | Where |
|---|---|---|
| table_type (BASE TABLE/VIEW) | Captured per engine via `table_facts` protocol (`app/services/db/base.py:109-124`; MySQL `mysql.py:141-172`; Postgres `postgres.py:187-218` relkind r/v/m/p; SQLite partial) | persisted as `kb_table_meta.table_type` (`knowledge_catalog.py:24`) |
| table_role + grain | DONE (P-SV) — name-hint regexes outrank structure, FK in-degree, business-key inference | `catalog_indexer.py:676-829`, `kb_table_meta.grain/table_role`, migration 099, `scripts/backfill_grain.py` |
| authority + grain enforcement | DONE — refuse/clarify/repair semantics | `source_verification.py:203-314`, enforced `nl_answer_service.py:975-1042`, flag `NL2SQL_SOURCE_VERIFICATION_ENABLED` |
| date coverage | DONE — probed windows `{date_column, min_date, max_date}` | `kb_table_meta.coverage_json`, `catalog_coverage_refresh.py` |
| time-aware reroute | DONE (time only — **the gap: no scope signal**) | `table_retrieval.py:558-624 reroute_uncovered`, call site `nl_answer_service.py:708-729` |
| retrieval ranking | Deterministic weighted retrieval; coverage annotates, never filters | `table_retrieval.py:218-482 retrieve_for_question` (weights :49-50, :239-248) |
| enforcement boundary | Single unconditional gate with check registry + severity contract (pass/fail/unknown/not-applicable/unavailable) | `execution_gate.py:59-70 EXECUTION_CHECKS`, `enforce` :255+ |
| catalog dual-store | Source comments persisted verbatim (`source_comment`), LLM descriptions separated with confidence | `kb_table_meta.source_comment` :35, `description_source/confidence` :46-53 |
| human overrides | `ProjectCatalogOverlay` wins over auto-inference (grain/role precedent) | `knowledge_catalog.py:279-302`, `source_verification.py:74-78` |

**Conclusion: the classification layer should extend the proven P-SV pattern — a new dimension beside `grain`, not a new pipeline.**

## 2. The four true gaps

1. **System-vs-business classification** — app-platform tables (auth/session/artifact stores) enter the candidate menu because the catalog indexes every table it sees.
2. **Scope dimension** — no notion of global vs partitioned (regional/departmental) tables; nothing stops a regional partition from answering a company-wide question.
3. **Scope-aware reroute** — `reroute_uncovered` swaps on date coverage only; it can (and did) substitute a scope-incompatible table.
4. **Adaptive-planner disclosure** — 7 fail-open sites (`adaptive_planner.py:89-101`, `capability_router.py:214-216, 267-289, 353-355`, `replan_policy.py:29-46`) proceed silently; degradation never reaches the user.

## 3. Design

### 3.1 New module: `app/services/db/schema_classification.py`

Pure functions over catalog metadata (mirror `query_contract.py` shape: dataclasses + pure validators + a prompt block). No DB calls inside classifiers — callers pass metadata; keeps it unit-testable and engine-agnostic.

#### 3.1.1 System vs business: `classify_system_business(table_facts, column_meta, relation_edges) -> ClassVerdict`

Signals (ordered, each contributes confidence; NONE is a hardcoded table name):

- **S1 Engine namespaces (structural, per-engine map)**: qualified names under engine-provided schemas — `information_schema`, `pg_catalog`, `performance_schema`, `sys` (MySQL/SQL Server semantics differ; map maintained per *engine*, which is generic, not per customer), `INFORMATION_SCHEMA`, `sqlite_*`. → `class=system`, confidence 0.99.
- **S2 Credential/session column shape** (generic column-shape regexes, precedent `catalog_indexer.py:650-673` "zero domain keywords"): columns dominated by credential/session shapes (`password`, `token`, `secret`, `salt`, `session_key`, `refresh_token`, `expires_at` + `revoked`) with NO business dimension/fact shape. → `class=system`, confidence 0.7-0.9.
- **S3 Graph isolation**: zero FK/join edges (`kb_table_relation`) to any table that has business-shaped columns, AND zero domain-support evidence (`domain_support` in `table_retrieval.py:170-191`), AND table_role=`unknown`. → `class=system_candidate` (deprioritize; **never hard-exclude** — it stays in the catalog, selectable on explicit request), confidence 0.6.
- **S4 Source comment evidence**: verbatim `source_comment` matching platform patterns in the customer's own words is an *override input* for humans, not an auto-signal (comments are untrusted for auto-classification).

Output: `ClassVerdict{schema_class: system|business|unknown, confidence, signals: [...]}`. Persisted to `kb_table_meta.schema_class` + `schema_class_signals_json` (new columns).

#### 3.1.2 Scope: `infer_scope(table, columns, relations, family) -> ScopeVerdict`

- **P1 Partition-family detection (fully generic)**: cluster tables by **column-fingerprint similarity** (multiset of column name-shapes; Jaccard ≥ 0.7 after normalizing type families). A family of ≥2 tables with near-identical fingerprints and mutually exclusive name suffixes = partition candidates; the varying suffix token is the partition key *value* (e.g. same fingerprint, names differ by a trailing token → each table is one slice of one logical dataset). No domain vocab required.
- **P2 Dimension-column evidence**: a low-cardinality column (`kb_column_meta.stats_json` distinct count ≤ threshold, or `enum_values` populated) whose values **match the family's suffix tokens** (or whose `source_comment` carries geo/organizational semantics observed from the data, e.g. values are proper nouns clustering the same partitioning) → the family is partitioned BY that column; tables lacking the column but sharing the fingerprint are pre-filtered partitions.
- **P3 Aggregate reconciliation**: a family member whose fingerprint is a strict superset with an aggregate-shaped measure and NO partition column = the **global/rollup** sibling. This is the positive signal for "this table can answer company-wide questions".
- **P4 Row-count parity**: family members with comparable row counts are peer partitions; one member with ≈sum of peers' counts corroborates the rollup (advisory only).

Output: `ScopeVerdict{level: global|partitioned, partition_key: column|None, partition_value: str|None, siblings: [table], rollup: table|None, confidence, signals}`. Persisted to `kb_table_meta.scope` + `scope_signals_json` (new columns; `scope` is one of `global|partition:<key>` — the VALUE stays in signals, the column stores the partition key name only).

Question-side scope resolution `resolve_question_scope(question, catalog_scopes)`: tokenizes the question and matches against **observed partition values and dimension enum values** (P2) — never a region vocabulary. A question matching a partition value scopes DOWN; scope-universal terms matched against the rollup sibling's evidence scope UP. Unresolvable → `scope=None` (no constraint).

### 3.2 Enforcement (three layers, all reuse existing seams)

1. **Retrieval (annotation-first)** — `retrieve_for_question` gains a scope-compatibility term: partition tables are *penalized, not filtered*, for scope-universal questions; rollup/global siblings get the boost. Precedent: coverage "annotates, never filters" (`table_retrieval.py:437-462`). System-class tables: rank penalty `class=system_candidate`, full exclusion only for S1 engine-namespace tables (those are never business data).
2. **P-SV gate extension** — `source_verification._load_catalog_facts` (:81-137) already loads `{role, grain, authority}` per table and overlay-wins; scope joins the same dict; `verify_sources` gains: question resolved scope ≠ selected table's scope → same refuse/clarify/repair ladder as grain incompatibility, including **auto-repair by swapping to the rollup sibling** (mirrors the ProjectMetric repair precedent :218-234). SQL-prompt guidance block extended with scope lines (precedent :286-313).
3. **`execution_gate` check `scope_authorization`** — one tuple in `EXECUTION_CHECKS` (:59-70) + one flag-gated block in `enforce` (precedent: metric-binding block :332-364, catalog-hiccup → `unavailable`/degrade-not-fail :355-357). Verifies the FINAL SQL's table set against the resolved scope (defense in depth — catches paths that bypass selection, e.g. adaptive replans).

### 3.3 Scope-aware `reroute_uncovered`

Extension of `table_retrieval.py:558-624` (time-only today):
- New param `scope: ScopeVerdict | None` threaded from `nl_answer_service.py:595-602` (beside `parse_relative_window`).
- Substitute eligibility: `covers_window` AND scope-compatible — a scope-universal question substitutes only from `{global, rollup, partition-whose-data-covers-everything-proven}`; a scoped question substitutes only from matching-partition or global.
- **No-scope-compatible substitute → do NOT swap**; instead the selection proceeds and the P-SV/`scope_authorization` layers refuse-or-disclose loudly. Swapping into a wrong scope silently is the exact live failure; an honest refusal beats a convincing wrong report.

### 3.4 Adaptive-planner disclosure (gap 4)

`AdaptiveDecision.reason` already exists structurally (sites 1-3). Wiring:
- Collect every degradation (7 sites above) into the run result via the disclosures channel built for query-contract reconciliation (`report_synthesis.synthesize_report(disclosures=...)` → `ReportCardPayload.warnings`) — precedent Blocker-4, already persisted/rendered.
- Format: user-facing, honest, non-technical ("Planning step could not be revised automatically; executed the last verified plan" / "Ran with a partially parsed plan").
- Flag `ADAPTIVE_DISCLOSURE_ENABLED` (default **on** — disclosure is honesty machinery, not behavior change).

### 3.5 Engine parity for introspection

Standardize on the `table_facts` protocol (`db/base.py:109-124`) — the M-Schema fallback layer (`datasources/postgres_adapter.py:89-133`) currently misses `table_type`/comments/row counts:
- Implement missing `table_facts`: SQL Server (`sys.tables`/`sys.views` + `sys.dm_db_partition_stats`), Oracle (`user_tables` + `all_views` + `num_rows`).
- M-Schema renderer (`datasources/m_schema.py:61-143`) annotates `# Table: name (VIEW|BASE TABLE, ~rows)` so the fallback path sees the same classification inputs.
- Classification is computed at **index time** (`catalog_indexer._introspect_one_table` — where table_facts already flow) and at **backfill time** (`scripts/backfill_schema_classification.py`, mirror `backfill_grain.py`), so runtime pays zero introspection cost.

### 3.6 Storage changes (one migration, additive, flag-independent)

`kb_table_meta` new columns (beside `grain`/`authority_override`, model `knowledge_catalog.py:67-71`):
- `schema_class` VARCHAR(16) NULL — `system|business|unknown`
- `schema_class_confidence` FLOAT NULL
- `schema_class_signals_json` JSON NULL
- `scope` VARCHAR(64) NULL — `global` | `partition:<key_name>`
- `scope_confidence` FLOAT NULL
- `scope_signals_json` JSON NULL

`ProjectCatalogOverlay` gains `scope_override` + `schema_class_override` (precedent: grain/role overrides :279-302). Human override always wins.

## 4. What this design deliberately does NOT do

- **No hard exclusion of auto-classified system tables** (except engine namespaces) — misclassification must degrade to a ranking penalty, never silently hide data. Audit log records the verdict.
- **No name vocabularies** for regions/departments/domains — partition values come from observed data (enum stats, suffix clustering).
- **No runtime introspection on the query path** — classification happens at index/backfill time.
- **No behavior change behind the flags**: `SCHEMA_CLASSIFICATION_ENABLED`, `SCOPE_GATE_ENABLED`, `ADAPTIVE_DISCLOSURE_ENABLED` — ship off, replay-verify, then enable.

## 5. Stage plan (each independently reviewable)

- **2B-1**: migration + model columns + `schema_classification.py` classifiers + backfill script. Pure + classifier unit tests (synthetic schemas: platform-shaped tables, partition families, rollup siblings, credential tables, engine namespaces). *No pipeline behavior change.*
- **2B-2**: retrieval scoring term + scope-aware `reroute_uncovered` (scope=None keeps exact current behavior).
- **2B-3**: P-SV scope integration (refuse/clarify/repair ladder) + `execution_gate.scope_authorization` check.
- **2B-4**: adaptive-planner disclosure wiring into the disclosures channel.
- **2C**: engine tests (SQLite real; MySQL/Postgres/SQLServer via fixture-driven `table_facts`), synthetic multi-schema E2E (unfamiliar synthetic DB → correct table, correct scope), live replay.

## 6. Test matrix (2C acceptance)

| Scenario (synthetic, zero real-world names) | Expected |
|---|---|
| Unfamiliar DB: 6 business tables + 4 credential/session tables + 3 engine-namespace tables | platform tables `system` (S1/S2), excluded/deprioritized |
| Column-fingerprint family of 3 partition tables + 1 rollup sibling | partitions `partition:<key>`, sibling `global` (P1/P3/P4) |
| Question naming a partition value | scoped selection; sibling rollup NOT substituted |
| Company-wide question, partition table ranked first | P-SV repair swaps to rollup; if impossible → refuse/clarify, never silent wrong-scope answer |
| `reroute_uncovered` with date gap AND scope mismatch | no silent cross-scope swap; disclosure emitted |
| Adaptive planner unparseable/raise/bad-action | degradation appears in ReportCardPayload.warnings |
| Catalog hiccup during scope load | `scope_authorization` = `unavailable`, degrade-not-fail |
| Regenerate/unknown window edge cases | current behavior preserved (scope=None) |
