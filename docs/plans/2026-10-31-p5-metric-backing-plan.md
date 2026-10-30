# P5 — Governed Metric and Entity Backing: Implementation Plan v1 (2026-10-31)

Anchor audit: `docs/audits/2026-10-31-p5-p7-stage-a-audit.md`. Non-negotiables: one metric registry (MetricDefinition), no LLM-computed numbers, no second SQL pathway, P1 evidence taxonomy, P2 revisions on ontology mutation, P4 authorization inheritance.

## Invariants
- I1: A metric value is produced ONLY by deterministic code from MetricDefinition's declared binding, executed through the EXISTING gate (execution_gate.enforce) and executor used by NLAnswerService. No new SQL pathway.
- I2: An LLM may name/explain a metric; it may never supply the numeric result or the formula.
- I3: Metric results carry: metric id + version, definition echo, unit, aggregation, provenance (source table/column), and an execution trace. Unavailability is TYPED (`unavailable` + reason), never a fabricated number, never an exception leak.
- I4: `evidence_status` on MetricDefinition uses the P1 canonical vocabulary (verified/computed/inferred/proposed/unavailable). Ontology mutations (binding a metric to an object) go through the existing human bind endpoint (P2 revisions + P1 stamping already there).
- I5: Entity backing resolves ONLY confirmed identity crosswalks (P3 `crosswalk_summary` discipline); ambiguous/unconfirmed → typed unavailable; never destructive row merge — per-source records, never a synthetic merged row.
- I6: No hardcoded department/metric vocabulary anywhere.

## Tasks

### P5-T1 — Registry extension (migration 111)
Extend `MetricDefinition` (additive, nullable / server-defaulted — no destructive change):
- `time_column` (String 200 NULL), `dimensions` (JSON NULL, list of {column, label?})
- `currency` (String 8 NULL), `zero_denominator_behavior` (String 20 NULL, vocabulary = ratio_contract.ZERO_DENOMINATOR_BEHAVIORS)
- `filters_json` (JSON NULL, structured basis: [{column, op, value}] — op whitelist: eq/ne/gt/lt/ge/le/in/notnull)
- `evidence_status` (String 20, server_default 'verified' — existing CRUD writes are admin-authored), `version` (Integer, server_default 1, bumped on every mutation), `lifecycle` (String 20, server_default 'published', vocab draft|published|retired)
- `reviewed_by` (String 120 NULL), `source_object_refs` (JSON NULL — ontology object ids this metric backs)
Consumers: `datasource_metric_bindings`/glossary keep working unchanged (additive columns).
Tests: model defaults; version bump on PATCH via existing CRUD; lifecycle=retired excluded from glossary/bindings; filters op whitelist validation.

### P5-T2 — Ontology metric backing (the core)
`services/ontology/metrics.py` (NEW): `compute_metric(db, org_id, metric_id_or_name, *, filters=None, dimensions=None, actor) -> dict`
- Resolve: org-scoped, lifecycle=published, is_enabled; name match canonical (case-insensitive) then synonyms. Not found/disabled/retired → typed unavailable.
- Authorization: caller must be an org member (org wall); tool-level callers additionally respect the calling agent's bindings (P0) — the shared tool checks the agent is bound (read) to a domain the metric's source_object_refs belong to, OR the metric has no ontology refs (org-level metric). Unauthorized → typed unavailable (never 403 leak from a tool).
- Deterministic SQL: `SELECT <agg>(base_column) FROM base_table WHERE <filters_json AND caller filters>` (+ optional dimension GROUP BY in a follow-up task; v1: scalar aggregates only). Identifiers validated against the live catalog (kb table/column whitelist — reuse data.py identifier guard discipline).
- Execution THROUGH THE EXISTING GATE: run `execution_gate.enforce` (metric_binding check passes trivially — SQL matches the declared binding) then the same QueryService execution call NLAnswerService uses post-gate. If the gate fails → typed unavailable with the gate violations (never a number).
- Result: `{success, metric: {id, name, version, aggregation, unit, currency}, value, provenance: {datasource_id, base_table, base_column, filters_applied}, evidence_status, execution_trace: {sql, gate: "passed", duration_ms}}`.
`data.py` metric branch: `backing_kind == "metric"` → resolve backing_ref (metric id) → compute_metric → return the governed result (typed unavailable on any refusal). Provenance per P4 if the metric carries document evidence.
Bind trap fix: `PATCH /ontology/objects/{id}/bind` accepts `metric` ONLY if the referenced MetricDefinition exists, org-matches, and is published (else 400 with reason); entity bind requires at least one confirmed crosswalk for the object's sources (else 400). Never a permanent refusal.
Tests: e2e compute on a seeded datasource (real sqlite KB through QueryService + gate); not-found / disabled / retired / cross-org → typed unavailable; gate-violation path; bind trap; identical result via data.py branch and via compute_metric; version + definition echo in trace.

### P5-T3 — Shared metric tool
Agent tool `compute_metric` (registry, category ontology/analytics): args `{metric (name), filters?}` → compute_metric → governed result; schema tells the LLM it may REQUEST a metric but the number is computed deterministically. Registered like other ontology tools; degrades to honest refusal when unbound. Tests: tool returns governed result; refuses cross-org/unauthorized; never echoes an LLM number.

### P5-T4 — Entity backing (confirmed crosswalks only)
`data.py` entity branch: `backing_kind == "entity"` → resolve the object's bound table rows + `crosswalk_summary` confirmed matches → return per-source record groups keyed by confirmed crosswalk pairs (source identifiers preserved; NO merged/synthetic row); any unconfirmed-only ambiguity → typed unavailable listing what a human would need to confirm. Tests: confirmed-only resolution; unconfirmed → unavailable; cross-org isolation; no destructive merge (two sources → two record groups).

### P5-T5 — Registry hygiene
- `requires_approval` becomes real: CRUD PATCH with requires_approval=true and no reviewed_by → stays draft (lifecycle), excluded from glossary/bindings until a reviewed_by is set via the approval path.
- Retire semantics: lifecycle=retired → removed from glossary + datasource_metric_bindings + compute_metric refusals; version bumped.
- Tests: draft exclusion; retire propagation; version monotonicity.

## Acceptance
Real end-to-end: seeded datasource → MetricDefinition (published, reviewed) → ontology object bound (human bind endpoint) → agent tool `compute_metric` AND ontology read both return the SAME governed value with identical semantics, gate-passed trace, and an LLM caller cannot alter the number. Unauthorized / missing / retired / gate-fail → typed unavailable.
