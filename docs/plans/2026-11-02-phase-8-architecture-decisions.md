# Phase 8 — Architecture Decision Record

**Date:** 2026-11-02 · Status: proposed (spec phase) · Evidence base: Phase 7C review + repo inspection (paths/lines in the spec §2).

## ADR-1 — Authoritative binding model: AgentDataBinding

- **Existing:** `AgentApp.knowledge_bases` (JSON array, `agent_app.py:38`) is what the runtime reads (`get_bound_data_source_ids`, `data_source_runtime.py:130-141`); `AgentDataBinding` (`agent_data_binding.py:22-37`) carries superior semantics (`access_mode`, table/column filters, `row_filters`, `nl2sql_enabled`) but **zero runtime consumers**.
- **Decision:** AgentDataBinding becomes authoritative; `knowledge_bases` JSON becomes a derived compatibility projection rebuilt in the same transaction on every binding write.
- **Alternatives:** (a) keep JSON authoritative and add permission fields to it — rejected: JSON columns can't hold relational constraints (unique index, RLS, FK), and AgentDataBinding already exists (invariant: no duplicate subsystems); (b) new UnifiedBinding table — rejected: proven unnecessary (invariant: prove existing models can't support the requirement first).
- **Compatibility:** runtime falls back to JSON when an agent has no binding rows → zero-behavior-change until migration.
- **Security:** unique index + org-via-parent + fail-closed cross-org validation.
- **Migration:** T8-11 materialization for auto-bind users; idempotent; reversible (projection stays live).

## ADR-2 — SourceDescriptor contract

- **Existing:** descriptors exist only as prompt text ("Bound Data Sources", `data_source_runtime.py:832-1015`) — fields id/name/db_type/source_kind/readiness/queryable.
- **Decision:** serializer `list_agent_source_descriptors()` in `data_source_runtime.py` returning the same fields + `binding_id`/`access_mode`/`is_enabled`; one API (`GET .../source-descriptors`), one UI contract; the prompt section keeps consuming the same source of truth.
- **Alternatives:** separate descriptor service — rejected (second abstraction without demonstrated need).
- **Security:** org-scoped; readiness disclosure reuses existing honest-degradation notes.

## ADR-3 — Child-agent authorization

- **Existing:** child inherits parent's `bound_kb_ids` only; child is permission-checked as itself (`delegate_tool.py:1162-1176`, security comment `:1164-1165`).
- **Decision:** preserve exactly that; add parent run/agent references for provenance linkage only. A real AgentApp child resolves its OWN bindings; never the union.
- **Alternatives:** binding-object inheritance — rejected: widens child scope beyond its own configuration (violates "delegated agents cannot inherit unauthorized resources").
- **Security:** narrow-only inheritance; provenance references carry no authority.

## ADR-4 — Provenance persistence

- **Existing:** prov-v1 converters + envelope (`provenance.py`), currently written only on NL2SQL lineage rows; AgentRun (`agent_run.py`) has `caller_context` JSON (org inside), steps carry tool facts; `tool_calls_for_frontend` is a message-attached UI record.
- **Decision:** extend prov-v1 with a `run` context block; persist the envelope on new nullable `agent_runs.provenance_json` written by `AgentRunService.finalize_run` post-commit, fail-open (the 7C E1 pattern). Steps referenced by execution_ref, not duplicated. Org taken from `caller_context` (no unsafe org-column assumption — per 7C constraint).
- **Alternatives:** (a) provenance on AgentInvocation — rejected: invocations don't exist on all paths (chat turn recorder is the universal choke point); (b) new provenance table — rejected: run is 1:1 with its envelope; a table adds joins without value; (c) write-time validation registry — rejected: prov-v1 `validate_canonical` already gates every entry.
- **Compatibility:** historical rows have NULL provenance and remain readable; serialization deterministic.
- **Trust:** status verbatim; no CoT; no raw sensitive inputs; failed tools never emitted as successes; supports_answer still set nowhere.

## ADR-5 — Auto-bind migration

- **Existing:** `auto_bind_all_datasources` (default False) widens an agent's bound set to all org DB+file KBs (`data_source_runtime.py:1033-1068`); admin-gated PUT (7C B2); deliberate current behavior pinned by tests.
- **Decision:** three-state migration: flag honored → workspace migrated (materialize effective set into explicit bindings; runtime skips widening; state recorded in workspace settings) → flag deprecated (docs + warning) → flag removed only after zero non-deprecated readers and a full compatibility cycle.
- **Alternatives:** (a) immediate removal — rejected: silently removes access for existing users; (b) keep both widening paths forever — rejected: violates "explicit bindings authoritative".
- **Compatibility:** no silent widening (materialization copies exactly the effective set) and no silent narrowing (flag honored until migrated); migration idempotent and observable.
- **Security:** org-admin only; materialization is same-org by construction.

## ADR-6 — Revocation and cached contexts

- **Existing:** runtime resolves bindings per turn (no cache — verified); MCP carries no context (7C S4).
- **Decision:** revocation/disable effective next turn by construction; MCP context propagation (T8-7) brings MCP under the same per-turn resolution; no new cache is introduced, so nothing to invalidate.
- **Alternatives:** TTL cache with invalidation — rejected: adds failure modes for zero need.
