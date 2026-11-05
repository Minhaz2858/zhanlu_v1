# Zhanlu Phase 3 — Production Readiness: Implementation Roadmap & Record

(2026-10-31. Companion docs: production-readiness audit; durable-execution,
semantic-capability-discovery, multilingual, observability design docs.)

## Shipped (all TDD RED→GREEN)

| Workstream | Module(s) | Tests |
|---|---|---|
| P3.1 Durable execution & recovery decisions | `app/services/durable_execution.py` (NEW), migration `115_observation_evidence`, `ObservationRecord.evidence_ids`, `_extract_evidence_ids` in capability_router | `test_durable_execution.py` 12 |
| P3.2 Semantic capability discovery | `app/services/semantic_capability_match.py` (NEW), wired into `delegate_tool` alternative discovery | `test_semantic_capability_match.py` 9 |
| P3.3 Multilingual classification | zh markers merged into `execution_recovery._MARKERS`, `localized_recovery_hint`, CJK bigrams in `capability_fallback._tokens` | `test_multilingual_recovery.py` 12 |
| P3.4 Reliability metrics + provider state | `app/services/reliability_metrics.py` (NEW) + LIVE probe | `test_reliability_metrics.py` 6 + live probe evidence |
| P3.5 Observability & cost | `app/services/usage_analytics.py` (NEW), `app/routers/usage_analytics.py` (NEW, admin-only, org-scoped), AdminConsole **Usage** tab | `test_usage_analytics.py` 7 |
| G Cross-department scenarios | cross-module chains | `test_phase3_scenarios.py` 8 |

## Regression & build

- 37-suite backend sweep: **493 passed**; 2 failures verified pre-existing via
  git-stash A/B (multiagent prepass timing + parallel-tools schema pollution —
  fail identically on the clean tree). 1 mid-session self-inflicted regression
  (marker-dict duplicate keys wiping English tuples) caught by regression and
  fixed with a programmatic merge.
- Frontend: full suite = same 8 pre-existing foreign-WIP failures only;
  production build **exit 0**.

## Rollback

Revert the six new modules + router registration + migration 115 (guarded,
droppable) + AdminConsole Usage tab + delegate_tool semantic call site. No
changes to: NL2SQL gates, governance store, scheduler registry, AgentApp
schema, reviewer protocol (additive fields only).

## Security regressions (Part H) — explicit tests

- Recovery never bypasses approval (`test_security_recovery_never_bypasses_approval`).
- Fallback never selects unpublished agents (semantic + lexical).
- Department wall honored in semantic selection.
- Tenant isolation: org-2 never sees org-1 data (`test_tenant_isolation_no_cross_org_leak`).
- Delegation depth/cycle guards, risk-tier enforcement, permission fail-closed:
  pre-existing suites re-run green (governance_gaps, permission_fail_closed).

## Status categories

- **IMPLEMENTED:** all six workstreams above.
- **INTEGRATION-TESTED:** everything (deterministic mocks; ~90 new tests).
- **LIVE-VERIFIED:** provider probe evidence only (401 invalid key). No live
  multi-agent runs.
- **BLOCKED / DEFERRED:** live evaluation (invalid_api_key — needs a funded,
  valid key); durable queue + webhooks (separate phase); automatic node-level
  crash-resumer wiring (decision layer shipped, orchestration wiring future);
  cost anomaly detection (needs live volume).
