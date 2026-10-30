# P5–P7 Stage A Audit — Evidence-Backed Baseline (2026-10-31)

Method: two parallel code audits of the current working tree (P5 metric architecture; P6/P7 evolution+UI), plus document review (`docs/audits/2026-10-30-ontology-gap-audit.md`, P4 progress log). Every prior-audit claim re-verified; corrections noted. **Worktree caveat:** 127 dirty files / 5,104 insertions of UNCOMMITTED concurrent work touch exactly the NL2SQL enforcement layer P5 builds on (`query_contract.py` NEW, `execution_gate.py`, `nl_answer_service.py`, `semantic_store.py`, `metric_bootstrap.py`) — all P5 anchors cited below are working-tree anchors.

## P5 — Governed Metric and Entity Backing

### Exists and is enforced end-to-end (REUSE, do not rebuild)
- **MetricDefinition** (`models/metric_definition.py:19-44`): name/description/category, datasource_id, base_sql/base_table/base_column, aggregation enum (sum/avg/count/min/max/distinct_count/custom), display_name/unit/format_pattern/synonyms, requires_approval (unread), is_enabled. Org-scoped.
- **Full enforcement pipeline** (recent, working-tree): glossary ingests enabled MetricDefinitions as VERIFIED terms (`semantic_store.py:648-675`); `<verified-metrics>` prompt block (`nl_answer_service.py:1247-1270`, flag `METRIC_BINDING_ENFORCEMENT_ENABLED`); source-verification force-includes bound tables (`source_verification.py:158-245`); **pre-execution gate** `execution_gate.enforce` metric_binding check (`execution_gate.py:335-386`) via `datasource_metric_bindings` + `validate_sql_measure_aliases` + `validate_confusable_measures` (`query_contract.py:90-193, 291+`); post-execution measure fingerprints (`nl_answer_service.py:2255-2306`). Violations hard-fail.
- **Execution surfaces**: `POST /nl2sql/ask` (`routers/nl2sql.py:90`); agent tools `answer_from_database` (`db_tools.py:1856`), `ask_data_agent` (`delegation_tools.py:1758`); `query_ontology`.
- **Two registries, one-way bridge**: ProjectMetric (proposal workspace, `knowledge_catalog.py:327-357`, LLM bootstrap `status=proposed`) → human approval (`PUT /projects/{id}/catalog/metrics/{id}`, `project_catalog.py:616-633`) → `promote_to_metric_definition` (`metric_bootstrap.py:174-283`). No reverse sync, no retire.
- **MetricDefinition CRUD** (KB-scoped): `routers/knowledge_bases.py:509-644` + frontend `KbMetricsEditor.jsx`; Data Map tab (`projectCatalog.js:46-68`, `DataMapSection.jsx`).
- **P3 identity**: `services/identity/` resolver (4-tier, all `inferred`), review.py human gate (inferred→confirmed/rejected, append-only IdentityDecisionEvent), `crosswalk_summary` confirmed-only. Sole runtime consumer: `data.py:225-239` `cross_sources` provenance block. **No row-level resolution consumer exists.**
- **RatioContract vocabulary** (`services/db/ratio_contract.py`): MEASURE_GRAINS, ZERO_DENOMINATOR_BEHAVIORS, unit vocabulary — exists, unreferenced by MetricDefinition.

### Verified gaps (P5 scope)
- **G-P5-1 (governance trap)**: bind endpoint (`routers/ontology.py:138-173`) accepts `backing_kind ∈ (entity, metric)` but `data.py:48-56` permanently refuses them ("not yet supported"). Admins can create bindings that can never be served.
- **G-P5-2**: MetricDefinition lacks: dimensions, time column/semantics, currency, zero-denominator/null behavior, structured filters, evidence_status, version, lifecycle, owner/reviewer, source-concept refs. "Oldest row wins" on name conflicts (`query_contract.py:108-111`).
- **G-P5-3**: no ontology→metric computation path; no org-level shared metric tool; formulas not duplicated today but no consumption contract for agents outside NL2SQL.
- **G-P5-4**: no entity backing resolution over confirmed crosswalks.
- **Non-goal confirmed**: `domain_value.py` math is domain-ROI (est_value/CSAT), NOT the metric registry — leave alone, document.

### Corrections to the 2026-10-30 audit
- G5 said "declared, not implemented (stub)" — now FALSE in the working tree: the binding-enforcement pipeline is real and tested (`test_query_contract.py`, `test_p06_phase_b.py`, `test_semantic_enforcement_p0.py`). P5 extends, not creates.

## P6 — Continuous Ontology Evolution

### Exists (REUSE)
- **Sensing loop**: `sensing.py:172-175` (60s poll, `run_ontology_sensors`), started/shutdown in lifespan (`main.py:857-870/968-970`); act path = `notify_object_change` → fanout (notify-only, no mutation).
- **Sibling loop pattern**: catalog watchdog + coverage refresh (`main.py:817-849`) — the house scheduling style (asyncio lifespan; **no Celery/APScheduler anywhere**).
- **Live catalog drift**: `SchemaService.drift()` (proven `test_schema_shape.py:402-443`); dead twin `datasources/drift.py:detect()` (zero production importers).
- **Discovery**: `discover_bindings(db, org, apply=True|False)` (`discovery.py:331`), idempotent-ish (skips wired objects, `:386`).
- **Document versioning**: `record_document_version` (`document_ingestion/versioning.py:44-83`) — no watcher, nothing ontology-side reacts.
- **Spreadsheet ingestion**: tabular KBs staged for identity (`service.py:337-348`) — no ontology reaction.
- **Coverage**: `compute_coverage` (`coverage.py:21`), exposed on overview.
- **Drift baseline**: the catalog itself (KBTableMeta/KBColumnMeta); no SchemaSnapshot table.

### Verified gaps (P6 scope)
- **G-P6-1**: no scheduled/manual ontology reconciliation; only auto-mutation is `POST /ontology/discover`.
- **G-P6-2**: no stale-binding validation (vanished column never marks a binding; no stale field on OntologyObject).
- **G-P6-3**: no document-version watcher (new versions never trigger grounded re-extraction).
- **G-P6-4**: no impact analysis (which objects/properties/metrics/agents a change affects) — fanout graph exists but is notify-time only.
- **G-P6-5**: no idempotent change-state (no signature/dedup of detected changes).

## P7 — Ontology Exploration UI

### Exists (REUSE)
- APIs: `GET /ontology/objects` (:88), **`GET /ontology/links`** (:100), overview (:53), actions (:111), bindings (:122), revisions (:734, :752), coverage-value (:403).
- Frontend: `@xyflow/react ^12.11.3` **installed and completely unused** (ReactFlow — the natural graph renderer; no d3/mermaid needed); `EnterpriseProfile.jsx` (CoverageCard, ObjectsPanel+EvidenceBadge, SensorsCard, RevisionsFeed — local components), AdminConsole 7 tabs (incl. Doc Evidence, Identity).
- `evidenceStatuses.js` mirrors the P1 canonical taxonomy.

### Verified gaps (P7 scope)
- **G-P7-1**: zero graph rendering of ontology links; no graph aggregate endpoint.
- **G-P7-2**: no object detail explorer (properties/backing/crosswalks/citations/revisions in one surface).
- **G-P7-3**: no metric explorer surface (definitions scattered across KB editors + Data Map).
- **G-P7-4**: no source→object→metric traceability view; graph responses must respect P4 per-entry authorization (doc_ctx) and P0 permission_scope.
