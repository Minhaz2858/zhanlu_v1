# Phase 7C — Architectural Completeness Review (independent verification)

**Date:** 2026-11-02 · **Branch:** `fix/automation-chat-artifacts-ordering` · **Verified HEAD:** `19e4aab7f`
**Method:** read-only code inspection against actual files (every claim below carries a file:line reference), plus fresh execution of targeted regression suites in this session. No production code was modified for this review.

---

## 1. Executive assessment

Phase 7C is **architecturally accepted with documented deferred debt**. All six workstreams' headline fixes are present at HEAD and behave as specified. Two critical defects found during 7C (dead cleanup wiring D0; cross-org pinned-datasource wall B1) are genuinely fixed, with regression tests exercising endpoint-level behavior. No new critical or high defect was introduced by the fixes. Remaining issues are MEDIUM or DEFERRED and none block Phase 8 (several are Phase 8's subject matter).

## 2. Verified Git state and commit reconciliation

- Branch `fix/automation-chat-artifacts-ordering`, HEAD `19e4aab7f`.
- `git log --grep="Phase 7C"` returns **10 commits**: `b5407ebb7` (audit+plan), `7f27cd191` (D0), `d31fcd0a1` (B1/B2), `e01b3825d` (D-wiring), `b6eb164fb` (E), `2c2f57687` (C), `78473cf62` (A1), `db19e237d` (F), `3bc88d404` (fixture parity), `19e4aab7f` (architecture-doc record).
- **DISCREPANCY CONFIRMED** in the previous completion report: it states "9 commits" while listing ten hashes. The correct count is **10** (the report header simply miscounted; all ten listed hashes exist in history and their changes are present at HEAD).
- Working tree carries 241 foreign-WIP entries; none were touched by this review. Every 7C commit was verified hunk-by-hunk at commit time (foreign-dirty files staged via single-hunk extraction with staged-blob syntax checks).

## 3. Phase 7C completion verification (workstream by workstream)

### A — SemanticGuidelines vs BusinessRule — CONFIRMED IMPLEMENTED
- Authoritative model unchanged: `BusinessRule` (`app/models/business_rule.py`, CheckConstraints at L38-46: scope/status vocabularies).
- Marker namespace reserved: `app/routers/knowledge_bases.py:831` (create) and `:881` (PATCH) reject titles starting `[business-rule:` with 400.
- Bridge ownership: `app/services/ontology/business_rule_bridge.py:37` `MARKER_PREFIX = "[business-rule:"`, `marker_for()` L40, `_is_marker_title()` L44; sweep/sync reconciles only marker rows.
- Residual risks (DEFERRED, documented in 7C plan): no unique constraint on `(datasource_id, title)` for `semantic_guidelines` (`app/models/semantic_guideline.py` has none) → a concurrent double-sync can transiently duplicate marker rows (per-row upsert is idempotent; race window remains); org-wide rules materialize on all KBs at sync time — rules authored after a KB is created are not retro-materialized.

### B — Datasource auto-binding and authorization — CONFIRMED IMPLEMENTED
- **Cross-org pinned wall (B1):** `app/services/data_source_runtime/data_source_runtime.py:1489-1497` — pinned id from client-writable conversation metadata (`app/routers/agents.py:12792`, `conv.metadata_["data_source_id"]`) is honored **only** when `pinned.org_id == agent_app.org_id` and the agent org is present (fail-closed when org missing). Regression: `tests/test_datasource_pinning_org_wall.py` (4 tests, cross-org + no-org cases).
- **Workspace-settings authz (B2):** `app/routers/workspace_settings.py:41-45` (client-supplied org mismatch → 403), `:75` (`require_org_admin` on PUT). Frontend sends no `org_id` (`frontend/src/components/settings/WorkspaceDataSection.jsx:57` body is `{auto_bind_all_datasources}` only) → **no frontend breakage** (NO DEFECT).
- Auto-bind is a single runtime consumer (`data_source_runtime.py:1033-1068`, `_maybe_extend_with_workspace_auto_bind`); flag still default False (`workspace_settings_service.py:30-35`); deprecation documented, removal deferred.
- **V2/V3 inconsistency (CONFIRMED, DEFERRED as B4):** only the v3 path passes `pinned_data_source_id` (`agents.py:12802`); the v2 path has no pin mechanism — behavior differs between versions by design of the legacy code, documented in the 7C plan.
- No first-datasource fallback in the harness: `ask_data_agent` auto-picks only when **exactly one** bound DB (`delegation_tools.py:612-626`), validated against `bound_kb_ids` (`:596-603`); no automatic query-all exists anywhere in `data_source_runtime.py`.

### C — AuditEvent governance — CONFIRMED IMPLEMENTED
- NL2SQL audit rows org-stamped from the binding row: `app/services/nl2sql/__init__.py:659-669` (`_audit_org` resolved from `AgentDataBinding`, fail-open write preserved).
- Registry reconciled: `app/models/audit_event.py` — event types now include the 13 statically-written literal types + enterprise/resource/identity/document/business-rule/ERP vocabulary; sources include `enterprise`; outcomes include `completed|error|positive`. Legacy entries retained (contract test `test_legacy_registered_types_remain`).
- Drift detection: `tests/test_audit_event_registry.py` **greps the actual codebase** for `event_type="…"`, `event_source="…"`, `outcome="…"` literals and fails on unregistered values — future writers must register. Limitation (MEDIUM, documented): f-string dynamic families (`{resource_type}_{decision}` review events, `business_rule_{decision}`) are documented, not enumerable; the registry is documentation + contract test, not a write-time validator (intentional — legacy events must stay readable).
- Sensitive-data handling: `nl2sql/__init__.py` audit `detail_json` stores only `sql_hash`/counts/duration/decision (`:671-678`), never SQL text. Deferred: `db_tools` policy-denial auditing (foreign-dirty hot file).

### D — Resource lifecycle and orphan cleanup — CONFIRMED IMPLEMENTED
- **D0 (was critical):** `app/services/enterprise/cleanup.py:149-151` loads the resource row **by id regardless of `is_deleted`** — the org-verification load can no longer return None for a just-soft-deleted resource (previously this made production cleanup dead code; callers at `entities.py:428/612`, `automation_api.py:1347`, `mcp.py:275` all soft-delete before invoking cleanup).
- Deletion paths invoking cleanup: generic entity DELETE (`entities.py:612-621`, failure surfaced as `cleanup_error`, never reported as clean success), bulk `delete_many` (via id-returning `soft_delete_many_records`), dedicated automation DELETE (`automation_api.py:1347`), MCP disconnect (`mcp.py:275`), AgentApp deletion (cleanup type `agent_app`, `cleanup.py:215-229` — soft-deletes its `AgentDataBinding` rows).
- Watchdog: `_resource_exists` check before digest (`drift_watchdog.py:144`) + tombstoning of dead resources (run rows soft-deleted org-scoped, `:148-165`) — no more failed-run rows per cycle forever. Candidates are `(org_id, type, id)` triples (`:117-125`) — org-scoped; fresh `SessionLocal` per candidate (`:107`).
- Digest org-scoping: `onboarding.py` `_resource_digest` filters `KBTableMeta.org_id` — verified by `test_onboarding.py` and the phase-5 e2e fixture parity fix (`3bc88d404`).
- Transaction boundaries: cleanup runs on the caller's session **after** the authorized soft-delete commits; a cleanup exception is caught and surfaced (`cleanup_error`) without undoing the deletion (`entities.py:619-624`) — correct ordering verified by `tests/test_cleanup_post_delete_order.py`. Idempotency: re-running cleanup on an already-deleted resource is a no-op (soft-delete filters), test-covered.

### E — Canonical provenance — CONFIRMED IMPLEMENTED
- Lineage-only envelope: `app/services/db/nl_answer_service.py:3533-3566` — the raw `ontology` section remains gated on real planning provenance (`:3537`), while the prov-v1 envelope builds unconditionally from whatever evidence exists (`:3547-3564`), org-stamped with a real `execution_ref`. No evidence → no envelope; NULL lineage stays NULL.
- Honesty: `app/services/enterprise/provenance.py:376-380` and `:405-408` omit `evidence_status` when the source carries none (no invented "proposed"); real rows pass status verbatim.
- Tri-state integrity re-verified: `supports_answer` is set **nowhere** in production (`grep` over `app/`); `from_sql_lineage` sets `retrieved=True, influenced=True` only because the executed statement actually read those tables (`provenance.py:273-279`) — the one fact-based influence claim.
- Serialization is deterministic for unchanged input (pure wrap, `provenance.py:260-264`).
- Residual (MEDIUM, deferred): AgentRun-side canonical envelopes do not exist yet — AgentRun org attribution lives only in `caller_context` JSON (`agent_run.py:34`); this is Phase 8 Workstream B.

### F — Business Context Snapshot — CONFIRMED IMPLEMENTED
- Planning narrowing live: `nl_answer_service.py:745` passes `datasource_id=kb_id` (Phase 3B's narrowing was previously dead code).
- Agent-bound rule narrowing: `business_context.py:303-306` (narrow-only; org-wide rules pass; unresolvable agent preserves prior behavior), resolution `:383-399` (org-scoped AgentApp lookup).
- Rejected processes excluded: `business_context.py:183-185` (NULL-safe `or_` filter).
- Metric gates: `business_context.py:337-339` (`is_enabled` + `lifecycle == "published"` — parity with the NL2SQL planning path at `:635-637`).
- Org isolation: every candidate query filters `org_id` (§2A foundation, unchanged).

## 4. Second-order architectural findings

| # | Severity | Finding | Evidence | Blocks P8? |
|---|---|---|---|---|
| S1 | NO DEFECT | Frontend workspace-settings contract intact after authz change | `WorkspaceDataSection.jsx:57` sends only the flag | No |
| S2 | MEDIUM / DEFERRED | No unique index on `(datasource_id, title)` in `semantic_guidelines` → concurrent bridge syncs can transiently duplicate marker rows (per-row upsert idempotent; next sweep reconciles) | `app/models/semantic_guideline.py` (no UniqueConstraint) | No |
| S3 | MEDIUM / DEFERRED | V2 has no pinned-datasource path (v3-only) — cross-version behavioral inconsistency | `agents.py:12802` (only caller) | No (P8 WS-A unifies) |
| S4 | MEDIUM / DEFERRED | MCP path bypasses `prepare_data_source_runtime` entirely — `fastmcp_app.py:48` calls `call_tool(name, args, context=None)` with no bound-KB context; data tools hit the no-context fallback | `app/services/mcp_server/fastmcp_app.py:48`; fallback `data_source_runtime.py:253-261` | No (P8 WS-C scope) |
| S5 | LOW | `ChatTurnRecorder.tool_call` does not persist `duration_ms` though `agent_run_steps` has the column | `harness/turn_recorder.py` vs `agent_run_step.py` | No (P8 WS-B scope) |
| S6 | LOW | `AgentRunService.finalize_run` does not snapshot the `tool_calls` JSON on the chat path (docstring implies it) | `harness/run_service.py:327-347` | No (P8 WS-B) |
| S7 | NO DEFECT | Cleanup partial-failure semantics honest (`cleanup_error` surfaced, deletion not undone) | `entities.py:619-624` | No |
| S8 | NO DEFECT | Watchdog tombstones are org-scoped soft-deletes of run rows only; EnterpriseOnboardingRun history preserved | `drift_watchdog.py:148-165` | No |

No manufactured defects; S2-S6 are pre-existing or inherent-to-design items that Phase 8 addresses.

## 5. Security and multi-tenant review

- Every 7C-changed authorization path is org-scoped server-side and fail-closed where a client-controlled id is involved (B1), fail-open only where telemetry would otherwise be lost (C1 audit write, provenance serialization).
- Negative tests exist for: cross-org pin (`test_datasource_pinning_org_wall.py`), cross-org workspace settings (`test_workspace_settings.py`), cross-org binding lookup (`test_agent_datasource_wiring.py`), cross-org onboarding/cleanup (`test_onboarding.py`, `test_enterprise_cleanup.py`).
- Department metadata never grants access (BCS project section is metadata-only — unchanged from Phase 2A tests).

## 6. Transaction and lifecycle review

Cleanup ordering, idempotency, and partial-failure surfacing verified (§3-D). Watchdog tombstones use their own session and never corrupt the scan. Provenance envelope build is fail-open and cannot break the telemetry row (`nl_answer_service.py:3565-3566`).

## 7. Provenance and audit governance review

Registry ↔ writer reconciliation is mechanically enforced for literal values and documented for the two dynamic families. Historical rows remain readable: legacy event types retained; pre-7C lineage rows without a `provenance` key are unchanged (contract test `test_historical_rows_without_canonical_section_unchanged` updated to the 7C semantics while keeping legacy-field assertions).

## 8. Test coverage and regression evidence

Executed fresh in this review session (2026-11-02):

```
cd backend && python3 -m pytest tests/test_cleanup_post_delete_order.py \
  tests/test_deletion_paths_cleanup.py tests/test_drift_watchdog_deleted_resources.py \
  tests/test_list_links_soft_delete.py tests/test_enterprise_cleanup.py \
  tests/test_onboarding.py tests/test_canonical_provenance.py \
  tests/test_prov_v1_adoption.py tests/test_lineage_only_provenance.py \
  tests/test_nl2sql_audit_org.py tests/test_audit_event_registry.py \
  tests/test_guideline_marker_guard.py tests/test_semantic_integration.py \
  tests/test_workspace_settings.py tests/test_datasource_pinning_org_wall.py \
  tests/test_agent_datasource_wiring.py tests/test_general_assistant_datasource_guard.py \
  tests/test_business_context.py tests/test_bcs_runtime_integration.py \
  tests/test_nl2sql_ontology_planning.py tests/test_nl2sql_ontology_phase3b.py \
  tests/test_governance_7b.py tests/test_enterprise_knowledge_6f.py \
  tests/test_p7_enterprise.py tests/test_ontology_core.py tests/test_document_review.py \
  tests/test_document_access.py tests/test_enterprise_e2e_phase5.py \
  tests/test_ontology_p0_governance.py -q
→ 251 passed, 0 failed (26.13s)
```

Coverage quality: endpoint-level tests exist for the pin wall, workspace authz (TestClient with dependency overrides), cleanup API wiring, and audit org stamping; service-direct tests cover BCS/provenance/bridge. Gaps (acceptable for acceptance): no concurrency tests (S2 race), no v2-path pin test (no such path).

## 9. Confirmed remaining defects

None at CRITICAL or HIGH. MEDIUM/LOW items: S2 (sync race window), S3 (v2/v3 pin inconsistency), S4 (MCP bypass), S5/S6 (provenance recording gaps) — all DEFERRED into Phase 8 scope.

## 10. Suspected risks requiring further investigation

- Bridge sync under multi-worker Celery with two rules disabled concurrently (requires live multi-worker test; static analysis says last-write-wins per row, no corruption).
- `AgentApp` rows predating the org column backfill may carry `default-org` — the pin wall treats unknown org as fail-closed, but BCS agent resolution would miss such rows (narrow-only, safe).

## 11. Deferred technical debt

As documented in `docs/plans/2026-11-02-phase-7c-implementation-plan.md`: A2 (unique index), A3 (new-KB org-wide rule materialization), B4 (v2 pin), B-flag removal, C3 (db_tools denial audit), F5 (KG context consolidation), AgentRun provenance envelopes.

## 12. Architecture invariants that must remain unchanged

Org isolation server-side; authorization never inferred from project/department membership; agent selects among authorized sources, harness enforces; no default/first datasource; no query-all; BusinessRule authoritative; proposed never auto-verified; rejected excluded; verified ≠ enabled; retrieved/influenced/supports_answer separate; observations never promote evidence; historical audit/provenance readable; unsupported capabilities reported honestly; failures visible/bounded/recoverable; no LLM-fabricated facts.

## 13. Final acceptance verdict

**ACCEPTED WITH DEFERRED DEBT.** Phase 7C's ten commits are present and verified at HEAD; both critical fixes (D0, B1) are real, tested, and endpoint-level; no critical/high defect remains; the deferred items are enumerated with evidence and are carried into the Phase 8 specification. Phase 8 may proceed.
