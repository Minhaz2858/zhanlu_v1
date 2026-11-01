# Zhanlu Enterprise Intelligence — Implementation Roadmap

**Companion to:** `docs/plans/zhanlu-enterprise-intelligence-target-architecture.md` (specs S1–S5 referenced below).
**Branch baseline:** `fix/automation-chat-artifacts-ordering` @ `010f282f6`. All phases additive; no rebuilds of working infrastructure; every phase is flag-gated or additive-migration only. Git discipline: explicit file staging only (branch carries ~217 foreign WIP entries).

---

## Phase 0 — Architecture Prerequisites (security & data hygiene) — MUST BE FIRST

**Objective:** close the verified holes that shared context would amplify.
**Files/services:** `app/routers/llm.py` (`resolve_message_project_id` :92-142), `app/routers/agents.py:6913-6948` (conversation project resolution), `app/routers/entities.py` (add delete hook), new `app/services/enterprise/cleanup.py`.
**Changes:**
1. Ownership check on client-supplied `project_id`: allow iff admin, `created_by_id==user`, or `ResourceShare` grant — else 404 (existence-only today).
2. Delete-path hook: on KB/Datasource/MCP soft-delete → soft-delete catalog rows, mark ontology bindings `orphaned`, remove `AgentApp.knowledge_bases` JSON refs + `AgentDataBinding` rows, mark EBPM `EnterpriseResource`; audit `resource_deleted`.
**Security:** closes same-org cross-owner KB inheritance; prevents stale `backing_ref` resolution.
**Acceptance:** spoofed project_id → 404 (new tests); deleted KB leaves zero live binding references; all existing entity/tenant-scoping tests green.
**Regression scope:** `tests/test_entity_tenant_scoping.py`, project/KB router tests, `tests/test_tenant_rls.py`.

## Phase 1 — Enterprise Knowledge Model (Spec 1)

**Objective:** authoritative-model links + canonical provenance + `BusinessRule`.
**Files:** new `app/models/business_rule.py`, `app/services/enterprise/provenance.py`; additive migration (EBPM-style `_base_cols` idiom, follow 114); `Terminology.ontology_object_id` column; `Project.department_org_unit_id` nullable FK; backfill script importing staged SemanticGuidelines as `applies_to=sql` rows (bridge-only).
**Dependencies:** none (Phase 0 recommended first for clean data).
**Deliverables:** models + migration + envelope serializer with tests (`tests/test_business_rule_model.py`, `test_provenance_envelope.py`).
**Security:** rules born `proposed`, human-enabled only; envelope read-only.
**Acceptance:** envelope covers EBPM + doc + metric evidence in tests; guideline backfill idempotent.
**Regression scope:** ontology model tests, EBPM suites.

## Phase 2 — Business Context Service (Spec 2)

**Objective:** `app/services/enterprise/business_context.py` — retrieval pipeline, renderers, cache, flags (`BCS_ENABLED` default off).
**Files:** new service + `tests/test_business_context.py`; config flags in `app/config.py`.
**Dependencies:** Phase 1 (envelope, rules).
**Deliverables:** `build_business_context` + `render_prompt_block` + `render_tool_context` + unit tests incl. budget/failure/degradation cases.
**Security:** per-object policy intersection, fail-closed policy resolution, org-scoped queries.
**Acceptance:** payload ≤ budget; verified>proposed ordering; `none_available` honest block; cache invalidated by `context_version`.
**Regression scope:** none (new module) — pure addition behind flag.

## Phase 3 — Agent Runtime Integration (Spec 3)

**Objective:** wire BCS into ReAct/FSM/workflow prompts + tool context; delegation ceiling; NL2SQL bridge.
**Files:** `app/services/agent_prompts.py` (new block), `app/services/data_source_runtime/data_source_runtime.py` (tool-context population), `app/services/tool_handlers/delegate_tool.py` (`elevated` + `DELEGATE_PERMISSION_CEILING`), `app/services/db/retrieval_evidence.py` + `db/query contract` bridge, `app/config.py`.
**Dependencies:** Phase 2.
**Deliverables:** flag-off byte-identical A/B test; flag-on integration tests (ReAct + FSM); ceiling + elevation audit tests; NL2SQL ranking test (ontology-bound table wins).
**Security:** ceiling default after rollout; only verified/enabled rules reach SQL prompts.
**Acceptance:** Spec 3 criteria (1)-(3).
**Regression scope:** full agent suite (`tests/test_agent_*`, FSM tests), delegation tests, NL2SQL suites — largest phase.

## Phase 4 — Onboarding Pipeline & Evolution (Spec 4)

**Objective:** post-index pipeline, delete cleanup integration, drift watchdog, `context_version` bumps.
**Files:** new `app/services/enterprise/onboarding.py`; `app/routers/entities.py` (hook calls); `main.py` lifespan (watchdog loop); `onboarding_runs` migration; EBPM hook wiring (`enterprise_discovery.refresh_inventory` etc.).
**Dependencies:** Phases 1-2 (version consumption); Phase 0 (cleanup service).
**Deliverables:** end-to-end onboarding test (KB create → catalog → inventory → evidence → proposals → review queue); orphan test; drift test.
**Security:** system-actor org-scoped pipeline; proposals only; deletions audited.
**Acceptance:** Spec 4 criteria (1)-(4).
**Regression scope:** entity CRUD tests, catalog indexer tests, EBPM suites.

## Phase 5 — Observed-Execution Mapping (EBPM increment 6)

**Objective:** honest execution↔process linkage.
**Files:** additive nullable `process_id` on `AutomationExecution/AgentRun/WorkflowRun/Nl2sqlQueryLog` (one migration); new `app/services/enterprise/observations.py` (heuristic matcher, `observed_confidence`); `GET /ontology/enterprise/processes/{id}/observations` (honest-empty default).
**Dependencies:** Phase 1 (envelope); independent of Phases 2-4.
**Deliverables:** matcher + endpoint + tests; observed badge wired in `ProcessFlowCanvas` (`DISCOVERY_FILL.observed` exists).
**Security:** org-scoped reads; never auto-promotes `discovery_status`; audit on linkage writes.
**Acceptance:** no linked records → honest empty; linked execution renders badge with confidence; rediscovery never claims observed.
**Regression scope:** EBPM suites, execution-model tests.

## Phase 6 — Enterprise UX Restructure (Spec 5)

**Objective:** Business Overview / How We Work / Business Knowledge / Resources / AI Workforce / Administration IA.
**Files:** `frontend/src/pages/EnterpriseProfile.jsx` (reorganize existing tabs + copy), onboarding-status panel, AI Workforce directory page; build only — no backend beyond existing APIs.
**Dependencies:** Phase 4 (onboarding status data); Phase 5 (observed badges) optional.
**Deliverables:** drill-path UX tests; production build green.
**Security:** unchanged server-side gating (`require_org_admin`, isAdmin tab filter).
**Acceptance:** Spec 5 criteria (1)-(3).
**Regression scope:** frontend tests + build.

## Phase 7 — Consolidation & cleanup (final)

**Objective:** remove transition scaffolding.
**Items:** `auto_bind_all_datasources` deprecation path (warning → removal); legacy `project` name-string column retirements where safe; audit event-type registry enforcement; SemanticGuidelines stage removal after BusinessRule adoption.
**Dependencies:** adoption telemetry from Phases 3-6.

---

**Ordering rationale:** security (0) precedes any widened context; the knowledge model (1) precedes the service that reads it (2), which precedes runtime wiring (3) — the highest-regression phase lands once the contract is stable and flag-gated. Phases 4-6 parallelize after 2. Phase 5 is independent and may run alongside 3-4.

---

## DELIVERY STATUS (2026-11-01) — VERIFIED implementation, not the complete target architecture

The roadmap's phase numbering evolved during implementation. Mapping of roadmap phases → delivered milestones (all VERIFIED by committed tests on branch `fix/automation-chat-artifacts-ordering`):

| Roadmap phase | Delivered as | Commits |
|---|---|---|
| Phase 0 (ownership + delete cleanup) | 0A + 0B | `7cf8653e0`, `ba3f088e9` |
| Phase 1 (knowledge model: BusinessRule, provenance, links) | 1A + 1B.1–1B.3 | `f016c3995`, `92798a005`, `df1fed51d`, `20d5f99cd` |
| Phase 2 (BCS + runtime wiring) | 2A + 2B | `fe49848aa`, `c65b54d7f` |
| Phase 3 (ontology-aware NL2SQL + provenance adoption + governed pre-checks) | 3A–3D.2 | `ccfa0581a` → `2aabf0cb7` |
| Phase 4 (onboarding + continuous evolution) | Phase 4 | `8409193ac` |
| Roadmap "Phase 5" (observed-execution mapping) | 3E + Phase 5A/5B | `d6b33a2f2`, `a0a9a9c0e`, `3c80738a7` |

**Phase 5 (execution provenance, observation writers, drift watchdog, connector registry) — DONE 2026-11-01:** 5A `a0a9a9c0e`, 5B `3c80738a7`, 5C `71ad905d7`, 5D `ca774f9cd`, 5E `87dff0582`. End-to-end verification: `tests/test_enterprise_e2e_phase5.py` (4 scenarios). Config flags: `ONTOLOGY_OBSERVATIONS_ENABLED` (NL2SQL + AgentRun observations, OFF), `ONTOLOGY_DRIFT_WATCHDOG_{ENABLED,INTERVAL_SECONDS,MAX_RESOURCES_PER_CYCLE,RETRY_BACKOFF_SECONDS}` (OFF).

**Remaining gaps (RECOMMENDATIONS, NOT implemented):** frontend observed-badge wiring in `ProcessFlowCanvas`; BCS token-budget production tuning; connector adapters beyond the four registered types (no new integrations exist); column-level drift hashing; `execution_ref` on AgentRun-side canonical provenance is built by consumers (from_observed_execution) but not yet persisted into AgentRun rows; provenance-envelope writers on FSM/workflow paths.

**Enterprise Intelligence UX phases (this branch, spec-5 line):** 6A `5fe3fad8c` (observed-execution visualization), 6B `2891d8f75` (map overview), 6C `d87f2dd78` (drilldown chain), 6D `e0efe475e` (executive intelligence). **6E DONE 2026-11-01** — management drilldown, accountability & executive reporting: configurable 7/30/90-day window, blocked-action investigation view (fixed the 6D decision-value mismatch — the audit writer records `'block'`), AgentRun terminal outcomes as a separate performance segment, per-department process breakdown, executive snapshot HTML export (same computed payload as the UI). 6F recommendation (NOT implemented): full IA restructure of `EnterpriseProfile.jsx` tabs (the roadmap's original Phase 6 UX item), saved deep-links, blocked-action → rule/audit navigation links, i18n for the snapshot, native PDF rendering.
