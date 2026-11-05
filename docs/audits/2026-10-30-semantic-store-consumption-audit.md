# P0 Audit — Semantic Store Consumption Across the NL2SQL Pipeline

Date: 2026-10-30
Type: READ-ONLY audit (no code changed)
Method: two independent code-explorer passes (store surface + consumption trace), file:line evidence throughout
Question answered: is the Semantic Store actually consumed at SQL-generation time on every execution path — or only displayed?

---

## Executive verdict

The Semantic Store is **genuinely consumed** at SQL-generation time — this is NOT a "metadata page" problem. All four execution paths funnel through one canonical pipeline (`NLAnswerService._answer_impl`), which injects verified metrics, workload hints, domains, glossary, and join paths into SQL generation, and enforces them again at execution via `execution_gate`.

The audit identified **four real defects** (three different root-cause classes, matching the reviewer's hypothesis):

| # | Defect | Root-cause class | Severity |
|---|---|---|---|
| D1 | Delegation **sub-loop** authors raw SQL with schema + join edges but **no metrics, no workload, no P-SV gate** — relies on the execution gate alone | Enforcement bypass | HIGH |
| D2 | **`resolved_policy=None`** passed by the FSM caller, delegation fastpath, and (partially) ReAct — user access-policy *pre-generation schema filtering* absent on those paths (nl2sql router does it correctly) | Enforcement gap | HIGH |
| D3 | **Description provenance (`description_source`/`description_confidence`) has zero consumers** — the LLM cannot distinguish an LLM-guessed column description from a source-comment or human-verified one | Retrieval/usage gap (insufficient *use* of existing knowledge) | MEDIUM |
| D4 | The delegation **sub-loop bypasses the turn SQL budget** (direct `execute_query`), and the nl2sql router passes no `turn_id` (budget degrades to conversation scope) | Enforcement gap | MEDIUM |

Plus two **integrity/observability** findings: the "Columns described (12)" UI stat actually counts *relations (tables)*, not columns (`semantic_store.py:1195`); and verified-metric storage is split across two models (`ProjectMetric` approved-by-human vs `MetricDefinition` enabled) whose sync path needs verification.

## 1. The funnel — where every path converges

All four routes reach `NLAnswerService._answer_impl`:

| Entry | Chain | Evidence |
|---|---|---|
| A. ReAct (v3 chat) | `query_database` tool → wraps SynexiaFSM → FSM data node → NLAnswerService | `agents.py:12729-12732`; `query_database_tool.py:185,309-316`; `capability_router.py:736-750` |
| B. FSM | `_execute_nl2sql_node` → NLAnswerService (fallback `DataSnapshotService.nl2sql` generates SQL *without executing* when no KBs) | `capability_router.py:709-750` |
| C. Delegation | `_ask_data_agent` → `_try_nl_fastpath` → NLAnswerService (`delegation_tools.py:1138-1181`); on failure → **iterative sub-loop** (sub-LLM authors raw SQL → `execute_query`) | `delegation_tools.py:91-98, 254-260` |
| D. nl2sql router | `routers/nl2sql.py:85-108` → `compat.run_canonical` → NLAnswerService | `services/nl2sql/__init__.py:292-306` |

The store side has a unified read-only facade: `SemanticStore` (`services/db/semantic_store.py:259`, factory :1247, org fail-closed :1214) exposing glossary/metrics/joins/workload/domains/guidelines/stats with provenance tiers (VERIFIED/DISCOVERED/LEARNED, :8-30). The retrieval side has a second composition point: `table_retrieval.retrieve_for_question` (:218) with declared weights (plan 3.0, text 3.0, column 2.25, metric 1.5, domain 1.5).

## 2. The seven audit questions

### Q1. Does the planner retrieve verified metric definitions before generating SQL?
**YES — on every funneling path.** `nl_answer_service.py:1084-1107` injects `contract_prompt_block(datasource_metric_bindings(kb_id))` into the SQL prompt (flag `METRIC_BINDING_ENFORCEMENT_ENABLED`, default ON). Source: `query_contract.py:87-144` reads enabled `MetricDefinition` rows org-scoped with derived confusable columns (FALLAMOUNT vs FAMOUNT). **AND** enforcement is universal: `execution_gate.py:344-383` (`validate_sql_measure_aliases` + `validate_confusable_measures`) runs on every parsed statement via `QueryService.execute` (`query_service.py:126-130`) — used by both the pipeline (`:1343`) **and** the sub-loop (`db_tools.py:1091-1093`).

### Q2. Does schema selection use semantic table classifications?
**YES on the funnel (role/grain/authority via P-SV); NO scope dimension anywhere** (this is the Stage 2B design). `verify_sources` (`source_verification.py:203-314`) loads `{role, grain, authority}` + human overrides, drops archive twins, enforces grain compatibility with refuse/clarify/repair — runs inside `_answer_impl` (`nl_answer_service.py:970-1042`), so A/B/C-fastpath/D. The selection TOC carries `table_role`/`grain`/`domain_name` (`schema_linker.py:673-700`). **Bypass: C sub-loop** — sub-LLM picks tables from `describe_schema` (gets `table_role` annotations `db_tools.py:368-408` + schema-linker slice `:585-648`, but no gate).

### Q3. Are join paths used at generation or merely displayed?
**USED, multiple consumers**: DDL slice renders `-- Known join paths:` with relation_type + confidence (`schema_linker.py:419-471, 613-617` → prompt at `nl_answer_service.py:803-809`); join-partner guarantee pulls catalog-vouched partners into selection (`join_partner.py:63-75`, flag `NL2SQL_JOIN_PARTNER_GUARANTEE_ENABLED`); C sub-loop's `describe_schema` renders SchemaGraph edges (`schema_graph.py:197-205`); `execution_gate.join_reachability` (:454-466) verifies the final SQL's joins. **Gap (reviewer's Problem C is partially valid)**: `KBTableRelation` carries `relation_type` + `confidence` but no explicit verified/inferred flag and no fanout-risk annotation; cardinality fields exist (`source_cardinality/target_cardinality`, `knowledge_catalog.py:157-158`) but no consumer derives fanout warnings from them at generation time.

### Q4. Are business rules injected into every relevant execution path?
**AUTO-extracted rules: inert by design.** `business_rule_extractor.py:175-206` stages `is_enabled=False` `[auto]` guidelines; only a manual endpoint (`routers/knowledge_bases.py:741-750`) enables them. The single consumer, `semantic_store.format_guidelines_block` (:1147-1160, injected at `nl_answer_service.py:1069-1079`), filters `is_enabled=True` — so **authored/human-enabled rules DO reach all funneling paths; auto rules reach nothing until a human flips them**. This is deliberate (human-review gate like metric promotion), not a bug — but it means the ~12 columns' decode comments and enum dictionaries are dormant knowledge.

### Q5. Can the system distinguish verified from inferred definitions?
**YES for metrics/plans/glossary; NO for descriptions.** Provenance tiers exist (`semantic_store.py:8-30`; `description_source`/`description_confidence` set at `catalog_indexer.py:1797-1808, 1862-1881`). The LLM sees verification markers for verified metrics (`<verified-metrics>` block), verified plan reuse, and glossary answers — but `build_full_toc`/`get_selected_tables_ddl` pass **bare description text with no source/confidence markers** (`schema_linker.py:673-700, 957-972`), and `description_source`/`description_confidence` have **zero consumers outside the indexer** (grep-confirmed). This is D3 — the direct evidence behind the reviewer's Problem A.

### Q6. Can an incorrect table selection be rejected before SQL execution?
**YES, four layers on the funnel**: (1) P-SV refuse/clarify pre-SQL; (2) coverage reroute + no-existing-table fallback (`:696-729`); (3) `execution_gate` (table/column existence with suggestions, join_reachability, read_only, dialect, metric_binding) at every execution; (4) turn SQL budget. **Gaps**: C sub-loop (no P-SV, no budget — D1/D4) and `resolved_policy=None` (D2): `delegation_tools.py:1176-1181`, `capability_router.py:747-750` pass None while `answer_from_database` (`db_tools.py:1402, 1420-1432`) and the nl2sql router (`compat.py:296-306`) resolve real policies — the gate's `policy` check ("not-in-scope" pre-generation schema filtering) is inert on A-fastpath/B/C-fastpath.

### Q7. Does the same semantic knowledge reach ReAct, FSM, DataAgent, delegated execution?
**Matrix** (CONSUMED / PARTIAL / BYPASSED):

| Input | A ReAct | B FSM | C fastpath | C sub-loop | D router |
|---|---|---|---|---|---|
| Glossary/synonyms | ✅ (project_id=None) | ✅ (None) | ✅ (None) | ⚠️ partial (aliases via schema-linker) | ✅ (None) |
| Metric prompt block | ✅ | ✅ | ✅ | ❌ BYPASSED (gate-only) | ✅ |
| Workload hints | ✅ | ✅ | ✅ | ❌ BYPASSED | ✅ |
| Domains | ✅ | ✅ | ✅ | ⚠️ partial | ✅ |
| Descriptions (TOC/DDL) | ⚠️ partial, no provenance | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| Join paths | ✅ | ✅ | ✅ | ✅ (SchemaGraph) | ✅ |
| P-SV gate | ✅ | ✅ | ✅ | ❌ BYPASSED | ✅ |
| execution_gate | ✅ | ✅ | ✅ | ✅ | ✅ |
| User access policy pre-gen | ❌ None | ❌ None | ❌ None | ⚠️ deny-in-tools | ✅ |
| Turn SQL budget | ✅ | ✅ | ✅ | ❌ BYPASSED | ⚠️ conv-scope only |

Also: every path passes `project_id=None` for glossary resolution (`SemanticStore` factory org fail-closed but project-agnostic) — project-scoped overlays only load when a project_id is threaded; worth confirming this is intentional org-wide semantics, not an accident.

## 3. Root-cause classification (reviewer's three-way question)

- **Insufficient semantic knowledge?** Partially — the store is rich (facade + 8 subsystems), but scope/classification doesn't exist (Stage 2B design targets this) and description provenance is stored-but-unused (D3).
- **Incorrect semantic retrieval?** Not found. Retrieval paths (TOC, table_retrieval weights, schema-linker slice) consume the store correctly where they run.
- **Failure to enforce retrieved knowledge?** YES — the dominant class: D1 (C sub-loop bypasses metrics/workload/P-SV), D2 (policy None on 3 callers), D4 (budget bypass on 2 paths). The pipeline is strong; the side doors are weak.

## 4. Secondary findings

1. **UI stat mislabeled**: `stats()["columns_described"]` (`semantic_store.py:1195`) counts described *relations (tables)*, not columns — the "182 tables vs 12 columns described" gap in the UI overstates the coverage problem's magnitude but real column-description coverage should be measured properly.
2. **Dual metric storage**: `ProjectMetric` (status proposed/approved, `knowledge_catalog.py:305-330`) vs `MetricDefinition` (is_enabled, what `query_contract` reads). `metric_bootstrap.py:158` writes only `proposed`. The approved UI metric must be synced into `MetricDefinition` for the contract to consume it — the sync/promotion path needs a dedicated verification (P2 candidate).
3. **Metric attributes**: `ProjectMetric.bindings` JSON carries `{table, measure_columns, date_column, dimensions}` — grain/date/dimensions partially present; **no currency/scope/filters fields** (reviewer's Problem B is accurate; scope belongs in the Stage 2B scope design, currency/filters are schema additions).
4. **FSM no-KB fallback** (`capability_router.py:709-711`) generates SQL via `DataSnapshotService.nl2sql` **without executing** — acceptable (no execution, no result), but it bypasses ALL semantic machinery; worth a disclosure line when it fires.

## 5. Recommended P0 follow-ups (surgical, before/alongside Stage 2B)

| Action | Fixes | Effort |
|---|---|---|
| P0-1: Thread `resolved_policy` from FSM caller + delegation fastpath (mirror `answer_from_database`) | D2 | Small |
| P0-2: Route C sub-loop `execute_query` through the same pre-SQL P-SV + budget checks (or migrate the sub-loop to the fastpath) | D1, D4 | Medium |
| P0-3: Thread `turn_id` through `run_canonical` | D4 (router leg) | Small |
| P0-4: Carry `description_source`/`confidence` markers into TOC + DDL prompt lines | D3 | Small |
| P0-5: Fix `columns_described` stat to count columns; add true column-description coverage to stats | observability | Trivial |
| P0-6: Verify ProjectMetric→MetricDefinition promotion sync; add join fanout-risk derivation from cardinality fields | P2/P4 groundwork | Medium |

These are enforcement/retrieval-usage fixes on existing knowledge — independent of, and complementary to, the Stage 2B scope/classification design (which remains the P1-P3 answer to wrong-table selection).
