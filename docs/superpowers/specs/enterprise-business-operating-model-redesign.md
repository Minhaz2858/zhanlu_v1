# Zhanlu Enterprise Business Operating Model — Implementation Spec

Status: EXECUTION GUIDE (Stage 2 of master prompt). Supersedes the technical-only
Enterprise Profile. Builds on P0–P7 ontology platform (commit `c3ea68944`).

## 1. Product goals

Transform `EnterpriseProfile.jsx` from an ontology administration panel into a
**business operating model** that lets enterprise leadership answer five questions:
performance, risks/problems, root cause, recommended actions, and realized value.

Principle (from master prompt): **extend what exists; rebuild nothing that works.**
The Ontology is the foundation, not the only UX.

## 2. Personas

- **Executive** (CEO/COO/CFO): wants a 30-second read on performance + priorities.
- **Business analyst / ops manager**: investigates a situation, drills into entities.
- **Ontology maintainer / admin**: manages domains, objects, bindings, metrics, governance.
- **Decision owner**: reviews recommendations, approves operational writes.

## 3. Navigation

Single page `EnterpriseProfile.jsx`, redesigned as a **left rail / tab switch** with
six experiences. Default landing = **A. Executive Overview**. Preserve all existing
capabilities (coverage map, objects, revisions, PO drafts, sensors, graph) — none removed.

| Key | Experience | Source of truth |
|---|---|---|
| A | Executive Business Overview | new metric-compute endpoint + overview |
| B | Business Situation Room | graph + metrics + agent harness |
| C | Business Object Explorer | `/api/ontology/graph` (Business + Studio modes) |
| D | Decision & Action Center | escalations + PO-draft approval flow |
| E | Business Outcome Tracking | action lifecycle (honest states if missing) |
| F | Operational Health & Governance | overview + sensors + revisions + bindings |

## 4. Screen requirements (per experience)

### A. Executive Business Overview (default)
- Governed KPI cards: only metrics backed by configured sources + published
  `MetricDefinition`. Each card: value + unit, previous-period delta, reporting
  period, data freshness, metric definition popover, provenance (datasource/table),
  source availability. **Honest unavailable state** if not computable (reason shown).
- Business performance trends: recharts line/area for revenue & margin where a
  `time_column` exists (compute 2–N periods via the metric endpoint).
- Business priorities: detected situations from **sensing** (`/api/ontology/sensors`
  in breach) + a deterministic declining-profitability detector reusing
  `compute_metric` (no LLM figures). Each item: what/when/evidence/entities/impact/next-step.

### B. Business Situation Room
- Input a business question (free text). Investigate an entity by following
  graph links (`/api/ontology/graph`). Reuse **existing** agent harness
  (`ask_data_agent`) — do NOT build a new orchestrator.
- Output structure: question, verified findings (from metrics + doc evidence),
  affected entities, relationships, hypotheses (clearly labelled), impact, recommended
  investigations. Facts vs hypotheses vs recommendations clearly separated.

### C. Business Object Explorer
- **Business Explorer** (business users): search object, open, explore connected
  objects by following real links, inspect governed metrics, view evidence, discover
  actions. Edges come ONLY from backend links — no illustrative edges.
- **Ontology Studio** (admins): preserve existing graph + domain/object/link/binding/
  metric/identity/revision/health management. Existing endpoints reused as-is.

### D. Decision & Action Center
- Recommendations surfaced from sensing breaches + situational findings.
- Each: problem, evidence, affected entities, recommended action, expected impact
  (with provenance), risks, required approval, execution status.
- **All writes** go through existing governed actions (PO draft approval,
  `execute-approved`). No frontend/LLM direct DB writes.

### E. Business Outcome Tracking
- Lifecycle view: problem → evidence → recommendation → decision → approval → execution
  → outcome. Track via existing `procurement_po_drafts` (status), `escalations`,
  `ontology_revisions`, and audit events. If realized-value measurement infra is
  missing, show honest "outcome measurement not yet wired" state — never fabricate ROI.

### F. Operational Health & Governance
- Consolidate: data-source connectivity, KB health, catalog freshness, ontology
  coverage, metric health, document evidence, authorization, binding health,
  revision history, sensing health, agent execution health, approval status.
- Investigate the recurring `KnowledgeBase not found` sensing error: root-cause in
  sensor lifecycle/reference (do not merely hide it).

## 5. Component hierarchy (frontend)

```
EnterpriseProfile (tab shell)
├─ ExecutiveOverview        (A) → MetricCard, TrendChart, PriorityList
├─ SituationRoom            (B) → QuestionBox, FindingTree, EntityPanel
├─ BusinessObjectExplorer   (C) → ExplorerCanvas (Business) | OntologyStudio (admin)
├─ DecisionCenter           (D) → RecommendationCard[]
├─ OutcomeTracking          (E) → LifecycleTimeline
└─ OperationalHealth        (F) → HealthPanel[] (reuses existing cards)
```

Implementation approach: extract existing inline components (CoverageCard,
ObjectsPanel, RevisionsFeed, PoDraftsCard, SensorsCard, OntologyGraph) into the F
rail and the C Studio; add new components for A/B/D/E. Keep existing styling tokens
(stone palette, rounded-xl, text-[11px] captions).

## 6. Data contracts

### New endpoint — `GET /api/ontology/metrics/{metric_ref}/compute`
Reuses `app.services.ontology.metrics.compute_metric`. Query:
- `org_id` (optional, from user)
- `filters` (optional JSON; e.g. `{"sale_month":"2026-10","model_id":"X"}`)

Response = exact `compute_metric` dict:
```json
{ "success": true, "unavailable": false, "value": 1640.0,
  "metric": {"id","name","version","aggregation","unit","currency","evidence_status","depends_on_metrics"},
  "provenance": {"datasource_id","datasource_name","base_table","base_column","time_column","filters_applied"},
  "evidence_status": "...",
  "execution_trace": {"sql","gate":"passed","checks","duration_ms"} }
```
Unavailable example: `{"success":false,"unavailable":true,"reason":"metric 'X' is draft — not computable until published"}`.

### New endpoint — `GET /api/ontology/metrics`
Lists the org's governed metrics (published+enabled) with metadata, so the Executive
Overview can enumerate KPI candidates without hardcoding business schemas. Returns the
same `metrics` slice as `/api/ontology/graph`.

### Reused endpoints (no change)
`/ontology/overview`, `/ontology/graph`, `/ontology/objects`, `/ontology/links`,
`/ontology/bindings`, `/ontology/revisions`, `/ontology/po-drafts`,
`/ontology/escalations`, `/ontology/sensors`, `/ontology/identity/matches`,
`/ontology/actions/{id}/execute-approved`.

## 7. Permissions

All endpoints already enforce `get_current_user_required` + org scoping
(`_org_or_404`). New metric endpoints inherit this. No permission model change.

## 8. Error / empty / unavailable states

- KPI unavailable → card shows reason, no number.
- Empty org (no metrics/objects) → "No governed metrics configured yet".
- Broken API → toast + inline error, never blank section.
- Unauthorized source → fail closed (existing behavior).

## 9. Implementation sequence

1. Backend: `GET /metrics/{ref}/compute` + `GET /metrics` (additive, tested).
2. Frontend tab shell + Experience A (real KPIs) + F (consolidation) + C (Business/Studio).
3. Experience B (situation room, reuses agent harness + graph).
4. Experience D (decision center, approval-gated).
5. Experience E (outcome tracking, honest states).
6. Tests + regression + build + live verify (automotive scenario).

## 10. Architecture invariants (must hold)

- MetricDefinition remains the governed metric source; LLM explains, deterministic
  `compute_metric` computes. Never fabricate figures.
- No new ontology engine / graph DB / orchestration framework.
- All operational writes approval-gated via existing actions.
- P0–P7 services authoritative; append-only revisions/audit preserved.
- Extend APIs; add endpoints only where a demonstrated gap exists.

## 11. Acceptance criteria

- Enterprise Profile communicates actual business performance (real governed metrics).
- Business issues investigable via connected ontology entities.
- Metrics from deterministic computation; evidence/provenance inspectable.
- Recommendations grounded; operational actions respect approval/authz.
- Automotive profitability scenario works end-to-end on synthetic sandbox data.
- P0–P7 regression suites pass; frontend production build succeeds.
- No foreign-WIP file touched.
