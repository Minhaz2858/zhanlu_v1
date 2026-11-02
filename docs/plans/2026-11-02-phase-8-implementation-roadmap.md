# Phase 8 — Implementation Roadmap (ordered, TDD, independently testable increments)

**Companion to:** `docs/superpowers/specs/2026-11-02-phase-8-agent-harness-source-selection-provenance.md` (spec) and `docs/plans/2026-11-02-phase-8-architecture-decisions.md` (ADRs).
**Risk key:** 🟢 low · 🟡 medium · 🔴 high. Every task: RED tests first → smallest GREEN → focused regression → isolated commit (surgical staging for foreign-dirty files).

## Phase 8A — Binding model and contract consolidation 🟡

**T8-1 · Add `is_enabled` + unique index to AgentDataBinding** 🟢
- Files: `backend/app/models/agent_data_binding.py`; migration `1NN_agent_binding_enable.py` (explicit-column style, tenant RLS policy like migration 119; downgrade drops both).
- RED: `tests/test_agent_binding_model.py` — unique `(agent_app_id, datasource_id)` enforced; `is_enabled` defaults True; org inherited.
- GREEN: model change + migration. Regression: `test_agent_datasource_wiring.py`, `test_enterprise_cleanup.py`.
- Acceptance: migration up/down succeeds on a copy of the schema; duplicate binding insert raises IntegrityError.
- Rollback: downgrade migration (additive only). Dependencies: none.

**T8-2 · Binding write service + compatibility projection** 🟡
- Files: new `backend/app/services/agent_binding_service.py`; `data_source_runtime.py::get_bound_data_source_ids` (L130-141) consults enabled bindings when any exist, else falls back to `knowledge_bases` JSON.
- RED: service tests — create/enable/disable/revoke; projection `AgentApp.knowledge_bases` rebuilt atomically on every write; cross-org datasource → `PermissionError`; duplicate → conflict; zero bindings → empty bound set (no fallback to auto-bind at service level).
- GREEN: service + projection. Regression: runtime wiring suites.
- Acceptance: JSON projection always equals sorted enabled binding ids after any operation. Rollback: revert runtime read (JSON remains authoritative again). Dependencies: T8-1.

## Phase 8B — Backend binding APIs and authorization 🟡

**T8-3 · Binding CRUD endpoints** 🟡
- Files: `backend/app/routers/agent_apps.py` (or nearest existing agent-config router — verify at implementation time); register in `main.py` if a new router.
- API: `GET/POST/PATCH/DELETE /api/agents/{app_id}/bindings` + `GET .../source-descriptors` per spec §11.
- RED: TestClient matrix — cross-org 403/400; non-admin non-owner 403; duplicate 409; disabled excluded from descriptors; descriptor shape `{id,name,resource_type,source_kind,db_type,binding_id,access_mode,is_enabled,readiness,queryable}`.
- GREEN: endpoints delegating to T8-2 service. Regression: existing agent-config API tests.
- Acceptance: all negative-authorization cases fail closed. Rollback: remove router include. Dependencies: T8-2, D3 serializer.

## Phase 8C — Agent configuration and Resource Center UX 🟡

**T8-4 · DataSourcesSection binding editor** 🟡
- Files: `frontend/src/components/agent/DataSourcesSection.jsx` (currently writes `form.knowledge_bases` at L78-92); `AgentConfig.jsx` (fetch descriptors).
- RED: component tests — descriptors render (type/readiness/access), enable toggle → PATCH, remove → DELETE, no permission → read-only, fallback to JSON editor when descriptor API 404s (backward compat).
- GREEN: editor; keep `update({knowledge_bases})` path for non-migrated agents. Regression: existing AgentConfig tests; `npm run build`.
- Acceptance: user can bind multiple heterogeneous resources, see readiness, toggle, remove. Rollback: component reverts to JSON editor. Dependencies: T8-3.

## Phase 8D — Runtime source-selection integration 🔴

**T8-5 · prepare_data_source_runtime honors bindings + migration gate** 🔴
- Files: `data_source_runtime.py` `_prepare_data_source_runtime_impl` (L1418+) and `_maybe_extend_with_workspace_auto_bind` (L1017-1068).
- Behavior: binding-aware bound set (T8-2) replaces JSON read; auto-bind widening skipped when agent has ≥1 explicit binding AND workspace migrated; pinned-datasource org wall (7C B1) unchanged and re-tested.
- RED: `tests/test_runtime_binding_resolution.py` — per-turn resolution; revocation effective next turn; auto-bind not applied post-migration; pinned wall intact; zero bindings + no project → no data sources (honest).
- GREEN: minimal runtime change. Regression: FULL `test_datasource_pinning_org_wall.py`, `test_agent_datasource_wiring.py`, `test_general_assistant_datasource_guard.py`, `test_workspace_settings.py`, v2/v3 e2e scope suites.
- Acceptance: no default/first-datasource, no query-all anywhere (negative tests). Rollback: runtime read reverts to JSON (projection kept in sync). Dependencies: T8-2, T8-3.

## Phase 8E — Delegation and MCP authorization propagation 🔴

**T8-6 · Delegation provenance linkage (no authority transfer)** 🟢
- Files: `delegate_tool.py` (child ctx L1162-1176: add parent `agent_app_id`/`run_id` reference fields only).
- RED: child provenance carries parent_run_id; child bindings still its own; anonymous child inherits parent bound_kb_ids narrowed-only.
- GREEN: context fields. Rollback: revert context dict. Dependencies: T8-5.

**T8-7 · MCP context propagation** 🟡
- Files: `mcp_server/fastmcp_app.py` (L48 — accept and forward caller context), `data_source_runtime.py` fallback (L253-261) reads MCP-supplied context.
- RED: MCP call with context → scoped `ask_data_agent`; without context → today's honest fallback (not widened).
- GREEN: context plumbing. Regression: MCP tool suites. Rollback: revert. Dependencies: T8-2.

## Phase 8F — Canonical AgentRun provenance 🔴

**T8-8 · `agent_runs.provenance_json` migration + finalize choke point** 🔴
- Files: migration `1NN_agent_run_provenance.py`; `harness/run_service.py::finalize_run` (L327-347) — post-commit, fail-open envelope build (7C E1 pattern); `agent_run.py` model column.
- Envelope per spec §8: run block (org from `caller_context`, status verbatim, parent_run_id, binding refs) + prov-v1 entries from existing converters + step-derived tool execution_refs.
- RED: `tests/test_agent_run_provenance.py` — successful/failed/partial/paused runs; status verbatim; failed tool ≠ successful observation; missing evidence absent; org stamped from caller_context; cross-org entry rejected; two serializations identical; historical rows (NULL) readable; envelope failure doesn't break finalize.
- GREEN: finalize writes envelope. Regression: `test_stable_stream_m1.py`, chat e2e suites, harness suites.
- Rollback: stop writing envelope (column nullable; readers tolerate NULL). Dependencies: none hard; T8-5 for binding refs.

**T8-9 · Provenance read API** 🟢
- Files: `backend/app/routers/agent_runs.py` — `GET /api/agent-runs/{run_id}/provenance` (org-scoped, 403 cross-org).
- RED: endpoint tests (org match/cross-org/missing). GREEN: serializer. Rollback: remove route. Dependencies: T8-8.

## Phase 8G — Provenance inspection UI 🟢

**T8-10 · "Sources & evidence" panel** 🟢
- Files: new `frontend/src/components/chat/RunProvenancePanel.jsx`; wired into the chat message footer near existing TrustPanel patterns; consumes T8-9 API.
- RED: renders sources/tools/evidence/status; failed run shows incomplete parts; no envelope → honest "not recorded".
- GREEN: component. `npm run build` green. Rollback: hide panel. Dependencies: T8-9.

## Phase 8H — Auto-bind migration and deprecation 🟡

**T8-11 · Migration endpoint + state** 🟡
- Files: `workspace_settings.py` router (`POST /migrate-auto-bind`, org-admin); `workspace_settings_service.py` (state key `auto_bind_migrated_at`); idempotent materialization via T8-2 service.
- RED: materializes exactly the effective KB set; second run = no-op; non-admin 403; post-migration runtime skips widening (covered in T8-5).
- GREEN: endpoint. Regression: workspace + runtime suites. Rollback: state key removed; flag still honored. Dependencies: T8-2, T8-5.

**T8-12 · Flag deprecation** 🟢 — docstring/deprecation warnings + docs; REMOVAL deferred behind compatibility criteria (zero non-deprecated readers + one full release cycle). Dependencies: T8-11.

## Phase 8I — Integrated acceptance 🟢

**T8-13 · Verification matrix execution** — run every named test in spec §12 + the 7C slice + full enterprise regression + frontend build; record evidence in the progress log. Dependencies: all.

**Dependency graph:** T8-1 → T8-2 → {T8-3 → T8-4, T8-5 → T8-6/T8-7, T8-8 → T8-9 → T8-10}, T8-11 after T8-5, T8-13 last. 8A/8F are independent tracks.
