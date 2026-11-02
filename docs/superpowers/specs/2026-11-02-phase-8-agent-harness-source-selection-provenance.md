# Phase 8 — Agent Harness Source Selection, Explicit Resource Bindings & Canonical AgentRun Provenance — Architecture Specification

**Date:** 2026-11-02 · **Status:** SPECIFICATION (no implementation in this milestone) · **Prerequisite:** Phase 7C accepted (see `docs/audits/2026-11-02-phase-7c-architectural-completeness-review.md`)

---

## 1. Goals and non-goals

**Goals**
1. Make `AgentDataBinding` the authoritative, governed record of which resources an agent may access.
2. Migrate `auto_bind_all_datasources` users to explicit bindings with an observable, reversible transition.
3. Give users one UI surface to view/configure an agent's heterogeneous resources (databases + file KBs) with permissions.
4. Persist a canonical prov-v1 envelope per agent run at the existing finalize choke point.
5. Keep the Agent Harness split intact: the agent decides intent/selection/planning; the harness enforces org, ownership, bindings, RBAC, safety, provenance, budgets, audit.

**Non-goals**
- No second datasource-routing architecture; no new resource-discovery subsystem.
- No removal of `auto_bind_all_datasources` in this phase (deprecation + migration only).
- No chain-of-thought persistence; no raw sensitive tool inputs in provenance.
- No new provenance format (prov-v1 is extended, not replaced).
- No frontend standalone app — integration into existing `AgentConfig.jsx` / `ProjectDetail.jsx` surfaces.

## 2. Existing architecture inventory (verified evidence)

| Capability | Location | State |
|---|---|---|
| AgentDataBinding model | `backend/app/models/agent_data_binding.py` L22-37 | Rich fields (`access_mode`, `allowed_tables/columns`, `blocked_tables`, `row_filters`, `nl2sql_enabled`) but **not read by the runtime**; no unique index; no `is_enabled`; org via parent AgentApp |
| AgentApp.knowledge_bases | `backend/app/models/agent_app.py` L38 | Plain JSON array of KB ids — what the runtime actually uses |
| Bound-source resolution | `data_source_runtime.py` `get_bound_data_source_ids` L130-141, `prepare_data_source_runtime` L1370-1415 → `_prepare_data_source_runtime_impl` L1418+ | Per-turn resolution (no long-lived cache) |
| Auto-bind flag | `workspace_settings.py` model L20-29; `workspace_settings_service.py` L30-35; consumed only at `data_source_runtime.py:1033-1068` | Default False; org+app scoped; admin-gated PUT (7C) |
| Runtime descriptor | System-prompt section "Bound Data Sources" (`_build_data_source_prompt_section` L832-1015: id/name/db_type/source_kind/readiness/queryable) + `ctx_extras["bound_kb_ids"]` (L1733-1740) | Prompt-shaped, not a structured API object |
| AgentRun / steps / invocation | `agent_run.py` (org in `caller_context` JSON only, `parent_run_id` L33), `agent_run_step.py` (step rows with tool_name/args/status/duration), `agent_invocation.py` | ChatTurnRecorder (`harness/turn_recorder.py` L107-179) creates/finalizes runs; `run_service.py` `create_run_record` L101 / `append_step` L286-312 / `finalize_run` L327-347 |
| Tool-call records | `tool_calls_for_frontend` list (agents.py, attached to assistant msg L9102) + `_turn_rec.tool_call(...)` L8455-8466 (no duration_ms — S5) | Two parallel records, no canonical linkage |
| Delegation context | `delegate_tool.py:1162-1176` — child inherits `bound_kb_ids` only; child permission-checked as itself | Partial inheritance, no binding object |
| MCP | `fastmcp_app.py:48` — `call_tool(name, args, context=None)`; bypasses `prepare_data_source_runtime` | No binding context (7C audit S4) |
| Frontend | `AgentConfig.jsx` → `DataSourcesSection.jsx` (edits `form.knowledge_bases` L78-92); project "Resources" sheet in `ProjectDetail.jsx` | Edits the JSON array directly |

## 3. Gap analysis

1. **Two binding representations, one authoritative-less runtime.** The runtime reads `AgentApp.knowledge_bases`; `AgentDataBinding` rows exist with superior permission semantics that nothing consumes. Gap: authority.
2. **No enable/disable or revocation state** on bindings; removing a KB from the JSON is the only "revoke".
3. **No structured SourceDescriptor API** — descriptors exist only inside prompt text.
4. **No canonical run provenance** — evidence scattered across `tool_calls_for_frontend` (message JSON), `agent_run_steps`, and NL2SQL lineage; `supports_answer` never set; org not on AgentRun as a column.
5. **Auto-bind is a silent scope-widening** flag whose effective access is not materialized anywhere.
6. **MCP/delegation paths** do not pass binding context (S4) or pass only kb ids (delegate).

## 4. Architecture decisions (full ADR detail in `docs/plans/2026-11-02-phase-8-architecture-decisions.md`)

- **D1 Binding authority:** `AgentDataBinding` becomes authoritative. `AgentApp.knowledge_bases` remains readable as a compatibility projection, rebuilt on every binding write (same transaction) so the runtime keeps working unchanged during migration.
- **D2 Binding identity:** unique index `(agent_app_id, datasource_id)`; add `is_enabled` (Boolean, default True); org scoping stays via the parent AgentApp (no org column duplicated on the binding).
- **D3 Descriptor contract:** new read-only serializer `list_agent_source_descriptors(agent_app, user, org_id)` in `data_source_runtime.py` returning `{id, name, resource_type, source_kind, db_type, binding_id, access_mode, is_enabled, readiness, queryable}` — the same data the prompt section already computes, exposed as an API + UI contract.
- **D4 Revocation:** effective next turn (runtime resolves bindings per turn; no cache to invalidate — verified). MCP gains context propagation so MCP invocations scope identically.
- **D5 Delegation:** children that are real AgentApps resolve their OWN bindings (unchanged); anonymous sub-agents inherit the parent's `bound_kb_ids` narrowed by the child's own bindings when applicable — never widened (current behavior preserved, formalized).
- **D6 Provenance persistence:** new nullable `provenance_json` column on `agent_runs` (migration), written by `AgentRunService.finalize_run` — the single terminal choke point for every chat/delegated/queued path. Envelope = prov-v1 `{version, entries}` with `run` context block; entries reuse existing converters (`from_sql_lineage`, `from_document_proposal`, `from_process_evidence`, `from_ontology_provenance`).
- **D7 Failed/partial representation:** envelope carries `run_status` verbatim (`completed|failed|partial|paused`); failed tools produce entries only if they produced real artifacts; a failed tool is never emitted as a successful observation.
- **D8 Auto-bind migration:** a scoped admin endpoint materializes the auto-bind-effective KB set into explicit `AgentDataBinding` rows (idempotent), then records migration state in workspace settings (`auto_bind_migrated_at`); the flag stops widening scope once migrated; removal only after a full compatibility period with zero flag readers outside the deprecated branch.

## 5. Binding model and lifecycle

```
created (is_enabled=true) → enabled ⇄ disabled → revoked (row soft-deleted)
```
- Creation: binding API validates `datasource_id` exists, same org as the agent, not deleted (fail-closed), actor is org-admin or agent owner.
- Disable: kept but excluded from `get_bound_data_source_ids`.
- Revocation: soft-delete + `resource`-level cleanup not required (binding is not a resource), but the agent's next resolution excludes it immediately.
- Compatibility projection: every write updates `AgentApp.knowledge_bases = sorted(enabled binding ids)` — atomic in the same transaction, so the legacy runtime path and existing consumers (`Chat.jsx` inherited-KB fallback, tests) keep working.

## 6. Runtime source-selection contract

Unchanged harness split. `prepare_data_source_runtime` gains one input: the binding-aware bound set replaces the JSON-array read when bindings exist for the agent; auto-bind widening is skipped when the agent has ≥1 explicit binding AND the workspace is migrated (D8). The agent still chooses which bound source to use per turn; `ask_data_agent` validation against `bound_kb_ids` (delegation_tools.py:596-603) unchanged.

**Negative guarantees (test-proven):** no default datasource (zero bindings → zero data tools reachability, honest refusal), no first-source fallback, no query-all, cross-org binding attempts rejected at the API and re-validated at resolution.

## 7. Delegation behavior

- `delegate_tool` child context: keep `bound_kb_ids` inheritance (narrow-only) and ADD the parent's `agent_app_id` reference for provenance linkage (not for authorization — the child is still permission-checked as itself, per the existing security comment at delegate_tool.py:1164-1165).
- Child provenance envelope carries `parent_run_id` (already on `AgentRun`, agent_run.py:33) — traceability without authority transfer.

## 8. Provenance data contract (prov-v1 extension)

Envelope persisted on `agent_runs.provenance_json`:
```json
{"version": "prov-v1", "run": {"run_id", "org_id", "agent_app_id", "agent_name",
  "conversation_id", "status", "started_at", "finished_at", "parent_run_id",
  "bindings": [{"binding_id", "datasource_id", "access_mode"}]},
 "entries": [ ...prov-v1 entries, unchanged schema... ]}
```
- Entries: SQL lineage (existing converter), document evidence, ontology provenance, process evidence — each already org-stamped with real execution_refs.
- Tool provenance: synthesized from `agent_run_steps` (tool rows already carry name/args/status/duration); ref via `agent_run:{run_id}:step:{step_id}` execution_refs — steps are NOT duplicated into the envelope.
- Trust: `run_status` copied verbatim; missing evidence stays absent (no empty-envelope fabrication); cross-org entry rejection reuses `validate_canonical`; collection happens AFTER finalize commits, in its own try/except — provenance failure never corrupts the run transaction (same fail-open pattern as 7C E1).

## 9. Authorization model

Binding writes: org-admin or agent owner, same-org resource only. Binding reads: org-scoped. Runtime: bindings re-resolved per turn; disabled/revoked bindings excluded immediately. MCP: context must carry `agent_app_id`+`org_id`; invocations without verifiable context get the existing no-context fallback (honest limitation surfaced, not silently widened). Negative tests specified in §12.

## 10. Frontend design (existing surfaces only)

- `AgentConfig.jsx` → extend `DataSourcesSection.jsx`: render bindings from the new descriptor API (type icon, readiness, access_mode selector, enable toggle, remove), replacing the raw KB multi-select; keep the JSON-array write path as fallback for non-migrated agents.
- `ProjectDetail.jsx` "Resources" sheet: unchanged (binding happens on the agent page, discovery stays on resources).
- Provenance inspection: a "Sources & evidence" panel in the chat message (reads a new GET `/api/agent-runs/{id}/provenance`) showing selected sources, tools executed, evidence entries, and run status — matching existing `TrustPanel` patterns.

## 11. API contracts

- `GET /api/agents/{app_id}/source-descriptors` → D3 list (org-scoped, 403 cross-org).
- `POST /api/agents/{app_id}/bindings` `{datasource_id, access_mode?, allowed_tables?...}` → creates enabled binding + projection update (400 cross-org/missing, 409 duplicate).
- `PATCH /api/agents/{app_id}/bindings/{binding_id}` (enable/disable/permissions), `DELETE .../bindings/{binding_id}` (soft-delete revoke).
- `POST /api/workspace-settings/migrate-auto-bind` (org-admin) → materialization (D8), returns counts.
- `GET /api/agent-runs/{run_id}/provenance` → the envelope (org-scoped; 403 cross-org).
All additive; no existing endpoint breaks.

## 12. Testing strategy (exit criteria are per-suite green counts, not "tests pass")

**Binding/authorization matrix** (each = named test): one agent/one DB; multi-DB; DB+file KB; two agents different bindings; same org different projects; cross-org bind → 400/403; disabled binding excluded; revoked binding excluded; resource deleted after binding (cleanup already covers the resource side); zero bindings → honest refusal; MCP execution with/without context; delegated child uses own bindings.
**Dynamic selection:** single-source; multi-source; heterogeneous two-source; clarify-when-ambiguous; irrelevant bound source NOT queried; no query-all; no default fallback; missing source → refusal/clarification.
**Provenance:** successful/failed/partial/replanned/multi-tool/multi-source/delegated/child-failure runs produce correctly-stamped envelopes; SQL lineage/document/ontology entries present; missing evidence absent; cross-org execution ref rejected; serialization stable across two writes; historical rows (null provenance_json) readable.
**Regression:** v2, v3, NL2SQL, MCP/A2A, ontology/governance, document retrieval, existing auto-bind users (flag still honored until migrated), existing frontend workflows.
Exit criteria: every named test RED→GREEN; the 7C verification slice (29 suites, 251 tests) stays green; full enterprise regression green; frontend build green.

## 13. Failure handling

Provenance collection failure → logged, run remains correct (provenance is additive). Binding projection update failure → binding write rolls back (same transaction). Migration materialization is idempotent and re-runnable. MCP without context degrades to today's behavior (honest limitation), never to widened scope.

## 14. Security considerations

Cross-org binding rejected at API and re-verified at resolution; revocation effective immediately (no cache); delegation never widens; MCP context required for scoped behavior; provenance contains no raw SQL text/credentials/document contents (hashes and refs only, mirroring 7C audit fields); provenance endpoint org-scoped.

## 15. Acceptance criteria

1. `AgentDataBinding` rows are the sole authority consulted by `get_bound_data_source_ids` when present (JSON projection kept in sync — asserted by test).
2. Auto-bind migration endpoint materializes correct sets idempotently; migrated workspaces ignore the flag.
3. Every v3 chat/delegated/queued run finalizes with a valid prov-v1 envelope (or an honest absence), org-stamped, status-verbatim.
4. UI shows allowed resources, per-run selected sources, tools, evidence, and incomplete parts.
5. All §12 matrices green; no regression in the 7C slice; no cross-org access anywhere.
