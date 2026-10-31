# P0–P7 Integrated Acceptance Specification (Option C: suite + live demo)

- **Date:** 2026-10-31
- **Status:** APPROVED (design + spec + autonomous implementation authorized)
- **Branch:** `fix/automation-chat-artifacts-ordering`
- **Program context:** P5, P6, P7 already shipped, reviewed, and committed. This spec closes the remaining acceptance items and proves the phases work **together** as one governed enterprise intelligence system.

## 1. Objective & final chain

The deliverable must demonstrate **one complete chain**:

> Enterprise resources → Ontology → Governed metrics → Authorized execution → Verified evidence → Business answer → Traceable visualization.

The automated suite is the **mandatory engineering gate**; the live walkthrough is the **production-style demonstration** that the integrated architecture works in practice.

## 2. Scope & non-goals (architecture preservation)

Reuse the existing services; do **not** introduce:
- a new ontology engine,
- a graph database,
- a new SQL execution path,
- a competing metric registry.

Borrowed, unchanged-in-spirit capabilities: source discovery, ontology (objects/links/bindings/evolution), identity resolution (P3), document grounding (P4), governed metric compute (P5), deterministic execution (`execution_gate`), evidence/provenance, graph API (P7). Preserve the accepted P0–P4 invariants (fail-closed permissions, canonical evidence model, transactional revision history, cross-source identity, document grounding).

## 3. Approved decisions

- **D1 — Shared synthetic sandbox:** One reproducible fixture factory for both deliverables. **Use separate isolated database instances for automated tests and the live walkthrough; share seed definitions, not mutable database state.**
- **D2 — Live execution:** Real-services walkthrough is the mandatory demonstration. Full LLM-agent chat invocation is an *additional* demo, not an acceptance dependency.
- **D3 — Metric dependencies:** Explicit metric-to-metric dependencies represented in the ontology graph. Add `depends_on_metrics` **only after inspecting the existing `MetricDefinition` structure**. Validate references, organization boundaries, cycles, and version consistency. **Never silently accept circular dependencies.**
- **D4 — F5 safety:** Inspect the foreign-modified `metric_bootstrap.py` first. Make a minimal non-overlapping change only if safe. Otherwise use the service layer. **Never overwrite or commit another session's changes.**

## 4. Shared synthetic sandbox (D1)

A single fixture factory (`tests/integration/sandbox_seed.py`) defines the seed **definitions** (deterministic, fixed IDs/names). Two isolated DB instances are built from it:
- **Test DB** — ephemeral per-test (pytest-managed sqlite), torn down after each run.
- **Live DB** — a separate, persistent sandbox (distinct sqlite file or dedicated dev schema), never mutated by the test suite.

Seed domain (small but complete, clearly synthetic):
- `vehicle_models(id, model_name, segment)`
- `sales_transactions(id, model_id, sale_amount, cost_amount, sale_date)`
- `financials(id, model_id, period, overhead_amount)` — second source for cross-source identity
- **File KB** (spreadsheet of vehicle models) → cross-source identity
- **Document KB** with the gross-margin definition quote → document grounding + authorization
- Ontology objects: `VehicleModel`, `SalesTransaction`, `Financials`, `GrossMarginMetric` (metric-backed), linked by `links`.
- Governed metrics (see §7): `revenue`, `total_cost`, `gross_margin` (depends on the former two).
- Identity crosswalks: confirmed matches between `sales_transactions.model_id` and the file-KB vehicle list.
- Document evidence attached to the gross-margin definition, with org-scoped authorization so one viewer sees it and another does not (provenance-restriction test).

## 5. Execution order & gates

| Order | Workstream | Completion gate |
|-------|-----------|-----------------|
| 1 | P5 closure (F5/F6) | F5/F6 regression tests pass |
| 2 | Automated P0–P7 suite | All mandatory assertions pass |
| 3 | P6 live drift | Real transition + `drift` revision recorded |
| 4 | P7 enterprise validation | 500-object graph + permission tests pass |
| 5 | Live walkthrough | Complete business-question execution with trace |

## 6. Detailed requirements

### 6.1 P5 closure (workstream 1)
- **F5 (version bump):** Inspect `metric_bootstrap.py` first (D4). Because the file is currently foreign-WIP, the **safe path is the service layer**: ensure `MetricDefinition.version` is set/bumped by the shared create/update helper that both CRUD and bootstrap call, so re-bootstrap yields `version >= 1` and bumps on conflict — without touching the foreign file. **Never commit the foreign file.**
- **F6 (rollback re-validation):** A retired/rolled-back metric must **never** return an old computed number. At execution time, `compute_metric` revalidates the metric's **current** definition and lifecycle. Regression tests: retired / draft / rolled-back → typed `unavailable`; re-published → correct value; provenance echoes the current definition + version.

### 6.2 Automated P0–P7 suite (workstream 2)
One coherent flow over the sandbox, asserting all ten capabilities:
1. Heterogeneous source discovery (DB + file + document KBs).
2. Ontology object + relationship resolution.
3. Cross-source identity resolution (confirmed crosswalk).
4. Document-grounded evidence + citations.
5. Document-level authorization inheritance (two users, different visibility).
6. Governed metric computation (P5 path through `execution_gate`).
7. Deterministic execution + provenance trace (identical inputs → identical value + trace).
8. Ontology evolution after a source change (P6).
9. Revision history + rollback (P2/P5-F6).
10. Graph API + permission-filtered payload (P7).

**Plus the explicit constraints:**
- **Metric dependency correctness:** `gross_margin → revenue + total_cost`; if `total_cost` depends on underlying cost metrics, those edges are represented too. The graph must render these edges; circular dependencies are rejected, never silent.
- **P5 rollback safety:** asserted in the suite — retired/rolled-back metric returns `unavailable`, never a stale number.

### 6.3 P6 live drift (workstream 3)
On a KB **with a catalog baseline**, apply three distinct real schema changes and `reconcile(apply=True)`:
- **Removed column** → affected binding becomes `stale`.
- **Removed table** → affected binding becomes `broken`.
- **Unrelated additive column** → existing valid bindings remain `ok` (healthy, unchanged).
Plus: **report-only reconciliation (`apply=False`) does not consume pending changes** (signature ledger not advanced). Assert a `drift` revision (`change_kind="drift"`) is recorded for `stale`/`broken` transitions.

### 6.4 P7 enterprise validation (workstream 4)
- Seed ~**500 ontology objects**; assert the graph endpoint and frontend explorer do **not crash**; exercise **search, filtering, navigation, metric dependencies, and restricted provenance**.
- **Restricted provenance:** a non-authorized viewer's graph payload omits the document evidence the owner sees (per-viewer fail-closed).
- **Metric dependency edges** present and validated (with cycle prevention from §7).

### 6.5 Live enterprise scenario (workstream 5 / B)
Business question: *"What is the gross margin by vehicle model this month, and which models have experienced declining profitability?"*

Real-services walkthrough (D2) against the sandbox:
discover authorized sources → resolve vehicle/sales/finance concepts → identify the governed gross-margin definition → resolve confirmed cross-source identities → execute deterministic calculations → explain results using verified evidence → show provenance + source citations → display relevant ontology relationships → preserve execution + revision traces.

Clearly identify the synthetic sandbox; do not fabricate data or formulas. Full LLM-agent chat invocation is an optional additional demo.

## 7. MetricDefinition inspection & `depends_on_metrics` (D3)

**Pre-step (mandatory before any code):** inspect the current `MetricDefinition` columns/relationships.

Field design (additive, nullable JSON): `depends_on_metrics` — list of referenced metric ids/names. Validation rules enforced on write and at graph-build time:
- references resolve to existing metrics and are **org-matched** (no cross-org leakage);
- **cycle detection** via DFS over the dependency graph — circular dependencies are **rejected with a clear error, never silently accepted**;
- **version consistency** — a referencing metric requires its dependencies to be `published` and at a compatible `version`; incompatible → typed `unavailable` or rejected on bind.

Additive migration only; no destructive change.

## 8. Acceptance gate summary

| Workstream | Mandatory gate |
|-----------|----------------|
| P5 closure | F5 (service-layer version bump) + F6 (rollback re-validation) regression tests green |
| Automated suite | All 10 capabilities + dependency/rollback constraints pass |
| P6 live drift | 3 transitions correct + report-only does not consume + `drift` revision present |
| P7 enterprise | 500-object graph stable + search/filter/nav/dep/provenance tests + permission filter |
| Live walkthrough | End-to-end business question executed with full trace + provenance |

## 9. Deliverables (files)

- `docs/superpowers/specs/2026-10-31-ontology-p0-p7-integrated-acceptance-design.md` (this spec)
- `tests/integration/sandbox_seed.py` — shared, deterministic seed definitions
- `tests/integration/test_p0_p7_integrated.py` — automated suite (workstream 2) + P5 rollback/P6/P7 assertions
- Backend: `depends_on_metrics` additive migration + `MetricDefinition` validation (cycle/org/version)
- Backend: F5 service-layer version bump; F6 rollback re-validation in `compute_metric`
- Frontend: P7 graph explorer hardened for 500 objects + metric-dependency edges + restricted provenance
- Scripted live walkthrough (`scripts/` or `tests/integration/live_walkthrough.py`) for workstream 5
- Memory update marking the integrated-acceptance program complete
