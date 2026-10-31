# P0–P7 Integrated Enterprise Ontology Platform — Acceptance Progress Log

Date: 2026-10-31
Branch: `fix/automation-chat-artifacts-ordering`
Spec: `docs/superpowers/specs/2026-10-31-ontology-p0-p7-integrated-acceptance-design.md` (Option C)

## Scope of this commit

Closes the P0–P7 ontology program with a **verified, integrated system** rather than
a collection of features:

- Dynamic enterprise data sources (KnowledgeBase connectors — database / file / document)
- Governed ontology + cross-source identity resolution
- Document-grounded, permission-aware evidence
- Versioned + governed business metrics (`MetricDefinition` with `depends_on_metrics`, F5 version bump)
- Continuous ontology evolution (`evolution.reconcile`)
- Visual ontology exploration (`/api/ontology/graph` + metric-dependency edges)
- Auditable, deterministic business answers (governed `compute_metric` path only)

## Clean-worktree regression evidence

Run from the exact committed working tree (`python3 -m pytest` from `backend/`,
anaconda Python with SQLAlchemy pinned to `2.0.36`):

| Suite | Result |
| --- | --- |
| New integrated acceptance gate (29 tests) — `test_p5_f5_f6`, `test_p0_p7_integrated`, `test_p6_live_drift`, `test_p7_enterprise`, `test_p7_live_walkthrough` | **29 / 29 PASS** |
| Pre-existing P6 evolution regression (`test_ontology_evolution.py`) | **15 / 15 PASS** |
| Related ontology/metric regression (`test_ontology_graph`, `test_ontology_core`, `test_ontology_p0_governance`, `test_ontology_p1_batch`, `test_ontology_p1_evidence`, `test_ontology_p2_revisions`, `test_metric_backing`, `test_metric_registry_governance`) | **110 / 110 PASS** |

Combined acceptance + regression: **44 passed** (new + P6) and **95 passed** (broader
ontology/metric ring) — zero failures in the governed-ontology surface.

## Defects found and fixed during integration (`app/services/ontology/evolution.py`)

**D1 — `_impact()` crashed the whole drift detection.**
`_impact()` called `list_bindings`, which queries `agent_domain_bindings`. When that
table was absent/unreadable the exception propagated out of `reconcile`, marking the
source `failed` and silently skipping `_apply_drift`. Fixed: the bindings fan-out is
now best-effort (wrapped in `try/except`, logged at warning). Production-appropriate —
a binding lookup failure must never abort drift detection.

**D2 — a drifted source vanished from the report inventory.**
`reconcile()` appended a drifted source to `report["changes"]` but **never** to
`report["sources"]`. The source inventory therefore omitted every source that actually
had drift — an E1-style visibility gap. Fixed: a drifted source is now also appended to
`report["sources"]` with `status="changed"`. Verified no regression in
`test_ontology_evolution.py` (15 tests), whose cross-org isolation + idempotency
assertions still hold.

## Test-harness fixes (`backend/tests/sandbox_seed.py` + call sites)

- `run_business_question()` declining loop was summing **all months** (missing
  `sale_month="2026-10"` filter) → `Model X` was never flagged as declining. Fixed by
  pinning the current-month filter so the prior-month comparison works.
- `ontology_graph(...)` test calls passed `domain_id` implicitly; the FastAPI
  `Query(default=None)` default is a `Query` object, not `None`, which leaked into the
  SQL `WHERE ontology_objects.domain_id = ?` parameter. Call sites now pass
  `domain_id=None` explicitly.
- Seeded `OntologyObject` / `OntologyLink` / `IdentityMatch` rows inherited the model
  default `org_id="default-org"` (via `TimestampedBase`), so `list_objects(org="org-sandbox")`
  returned nothing. Added `org_id=ORG` to every seeded row.
- Added `AgentDomainBinding` + the `kb-sales` catalog baseline to the explicit test
  schema so the `agent_domain_bindings` query and the P6 drift path resolve.

## Foreign-session hazard (preserved untouched)

- `backend/app/services/knowledge_graph/metric_bootstrap.py` is **foreign-WIP (113
  insertions)** and was **never edited or staged**. F5 version-bump was implemented in
  the service layer (`MetricDefinition.before_update` listener) specifically to avoid
  overlapping with that file (D4).
- Only the 12 explicitly-listed paths were committed; `git add .` / `git add -A` were
  not used.

## Architecture preserved

No new engine, GraphDB, SQL path, or registry was introduced. The metric-dependency
graph reuses the existing `MetricDefinition` model + `ontology_graph` aggregate; drift
reuses the existing `reconcile` + `SchemaService.describe_all` path.
