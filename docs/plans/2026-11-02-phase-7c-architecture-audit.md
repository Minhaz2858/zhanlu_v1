# Phase 7C — Enterprise Architecture Consolidation Audit (2026-11-02)

Read-only audit at HEAD `df526032c`, branch `fix/automation-chat-artifacts-ordering`.
Every finding below is evidence-based (file:line verified); CONFIRMED vs SUSPECTED
is explicit. Foreign WIP status checked per file before planning (foreign-dirty:
`entities.py`, `data_source_runtime.py`, `nl_answer_service.py`, `nl2sql/__init__.py`).

---

## Workstream A — SemanticGuidelines vs BusinessRule

**Ownership verdict (CONFIRMED):** `BusinessRule` is authoritative. `SemanticGuideline`
(`app/models/semantic_guideline.py`) is a per-datasource retrieval representation;
`business_rule_bridge.py` materializes enabled+applies_to='sql' rules as marker rows
titled `[business-rule:{id}] {scope}`. BCS reads BusinessRule directly (zero
SemanticGuideline references) — marker staleness cannot affect BCS. The only
prompt consumer is `nl_answer_service.py:1246-1272` via
`SemanticStore.format_guidelines_block()` (`semantic_store.py:1187`).

**Lifecycle coverage (CONFIRMED):** all five transitions route through
`review_business_rule` → `maybe_sync_on_review` inside the review transaction
(`business_rules.py:145-146`); disable/reject/resubmit retract markers
(`sync_single_rule` soft-deletes non-eligible derived rows, bridge:121-123);
tests cover retraction + re-materialization (`test_semantic_integration.py`).

**Confirmed defects:**
- **A1 — marker-prefix guard missing on guidelines CRUD.**
  `POST/PATCH /{kb}/guidelines` (`knowledge_bases.py:812-877`) accept arbitrary
  titles. A manual row titled `[business-rule:...]` is swept by the next full
  sync (clobbered if it matches a live rule); PATCH-renaming a derived row
  breaks marker matching and leaves a stale, non-retractable prompt line.
- **A2 — race window (SUSPECTED, no DB constraint):** no unique index on derived
  marker rows; two concurrent syncs could double-create. `maybe_sync_on_review`
  is fail-open (bridge:162-166) — a failed bridge sync can leave a disabled
  rule's marker until the next manual sync.
- **A3 — known under-delivery (documented, deferred):** org-wide rules don't
  materialize on KBs created after the last sync (bridge docstring :22-24).

**NOT a defect (verified):** sync idempotency (per-row update-in-place, test
covered); manual-row sweep safety (title-prefix guard at bridge:148-154);
`datasource_ids` targeting (`_targets` bridge:59-67 — targeted rules materialize
per listed KB only); consumer-side scoping (`semantic_store.py:1171` drops rows
whose datasource_id ∉ requested kb_ids).

## Workstream B — Datasource auto-binding & authorization

**Confirmed defects:**
- **B1 — CROSS-ORG pinning (critical).** v3 appends a client-writable
  `conv.metadata_["data_source_id"]` to the bound set with **no org check**
  (`data_source_runtime.py:1485-1488`): `db.get(KnowledgeBase, id)` accepts any
  org's KB. A conversation owner can pin a foreign-org datasource into their
  agent's scope. Fix: org wall against the agent's org, fail-closed.
- **B2 — workspace-settings authorization.** `PUT /api/workspace-settings`
  (`workspace_settings.py:45-64`) requires only an authenticated user; `org_id`
  is a client-supplied query param (default `default-org`) — any user can flip
  `auto_bind_all_datasources` for any org. Fix: org wall from verified user +
  org-admin gate on writes.
- **B3 — doc/code mismatch (resolved in favor of code):** auto-bind unions ALL
  workspace KBs including file-kind (code :1045-1055) while the docstring says
  database-only. `test_agent_datasource_wiring.py` documents file KBs as a
  deliberate part of the runtime surface → behavior kept, docs corrected.
- **B4 (documented, deferred):** v2 never applies pinned datasource; v3 does
  (`agents.py:7578` vs `:12795`). Pre-existing inconsistency; changing v2 risks
  behavior regressions — documented only.

**NOT a defect (verified):** `nl2sql/compat.resolve_source` has no first/default
fallback (explicit refusals, `compat.py:204-205`); single-bound prefill only when
exactly one source; no automatic query-all execution loop; tool-level wall
`_require_kb_id` refuses unbound ids.

## Workstream C — AuditEvent governance

**Model:** `audit_event.py` — free-form `event_type`/`event_source`/`outcome`;
registry lists exist (:16-26) but nothing enforces them; org_id inherited from
TimestampedBase with server default `"default-org"`.

**Confirmed defects:**
- **C1 — nl2sql audit rows are mis-attributed tenants.** `_write_audit`
  (`nl2sql/__init__.py:655-681`) sets no `org_id` → falls to the
  `"default-org"` server default. Every other writer sets org_id.
- **C2 — registry/reality drift.** 18+ event_types written vs 13 registered;
≥9 registered types have zero writers; outcome vocabulary violated
(`"error"`, `"allowed"`, `"completed"`, `"partial"`); source vocabulary
violated (`"enterprise"` unregistered; `"ontology"` reused by erp/onboarding/
identity writers).
- **C3 (deferred):** db_tools policy-block denials are not audited
(`db_tools.py:1071` — returns error, no AuditEvent). db_tools is a foreign-dirty
hot file; documenting, not wiring this cycle.

**NOT a defect (verified):** no sensitive data in detail_json (SQL stays hashed;
raw SQL/question deliberately live in Nl2sqlQueryLog); truncation discipline
consistent ([:300]); denial-path auditing exists for permission denials, rule
blocks, NL2SQL denials; historical `f"{resource_type}_{decision}"` family is a
deliberate convention (7B tests depend on it).

## Workstream D — Resource lifecycle & orphan cleanup

- **D0 — CRITICAL: production cleanup is DEAD.** `_load_resource_row`
  (`cleanup.py:145-149`) filters `is_deleted == False`, but both wired call
  sites soft-delete FIRST (entities.py:562→577; mcp.py:264→276). The load
  returns None → `LookupError` → entities.py swallows (`pass`, :581-582) →
  **no cleanup ever ran via any production path**. Phase 0B tests called the
  service directly on live rows, so this never surfaced. Fix: load the row
  regardless of `is_deleted` (the load exists for org verification; the flag
  is irrelevant) + endpoint-order regression test.
- **D1 — watchdog spins forever on deleted resources.** `scan_once`
  (`drift_watchdog.py:105-148`) takes candidates from EnterpriseOnboardingRun
  triples with no existence check; a cleaned-up KB yields an empty-table digest
  ≠ last digest → `on_resource_changed` → `resource_missing` failed run row
  **every cycle, forever** (normal return → no backoff). Fix: existence check +
  tombstone run rows for missing resources.
- **D2 — dedicated automation delete bypasses cleanup**
  (`automation_api.py:1318-1342`) — the primary UI path.
- **D3 — bulk delete bypasses cleanup** (`entities.py` delete_many :402-415).
- **D4 — AgentApp deletion orphans AgentDataBinding rows** (`_cleanup_agent_bindings`
  matches only on datasource_id).
- **D5 — `list_links` returns retired links** (`queries.py:114-120` — no
  `is_deleted` filter; drilldown filters it, list doesn't).
- **D6 — `onboarding._resource_digest` filters KBTableMeta by kb_id but not
  org_id** (`onboarding.py:58-61`) — convention inconsistency, one-line fix.

**NOT defects (verified):** cleanup org-safety + idempotency solid (fail-closed
org check :81-84; all sub-cleanups org-filtered; re-run yields zero counts);
`datasource` cleanup support is honestly documented dead support (no endpoint
exists — do NOT invent one); evidence/rule/EnterpriseResource retention is
deliberate history-preservation; object-retire link retention is deliberate
(the bug is D5, the read filter).

## Workstream E — prov-v1 canonical provenance

**Writers today (CONFIRMED):** nl_answer_service lineage (gated on
`ontology_provenance`, :3532-3559), document_review (:97), enterprise_discovery
evidence items (:818). `resolve_execution_ref`/`entries_with_verified_execution_refs`
are contract+tests only — no production caller yet.

**Confirmed defects:**
- **E1 — canonical section skipped for lineage-only rows.** The whole prov-v1
  build sits inside `if ontology_provenance:` — a query with SQL lineage but no
  planning provenance gets NO canonical envelope despite `from_sql_lineage`
  being ready and `_org` already hoisted (:3520-3526). Fix: build the envelope
  whenever entries exist; keep the raw `ontology` section gated as before.
- **E2 — fabricated evidence_status in converters.** `from_document_proposal`
  (`provenance.py:376-378`) and `from_process_evidence` (:402) invent
  `"proposed"` when a foreign dict lacks any status field. Real rows always
  carry a status; fix to omit-when-absent (tri-state honesty).

**NOT defects (verified):** tri-state integrity — `supports_answer=True` is set
NOWHERE; `influenced=True` hardcoded only in `from_sql_lineage`;
execution_ref org wall holds (nl2sql by org_id column, AgentRun via
`_agent_run_org` caller_context verification); no confidence fabrication.
MetricDefinition.evidence_status is `nullable=False default="verified"`
(`metric_definition.py:56-57`) — the planning `or "verified"` fallback is dead
code for real rows, NOT a fabrication bug (verified and closed).

**Deferred:** AgentRun-side provenance envelopes (write-path risk; observation
records already carry the linkage; documented as next-milestone candidate).

## Workstream F — BCS consistency

**Consumers (CONFIRMED):** v2/v3 chat (`agents.py:7437/13575` via
`build_bcs_block`), explainability endpoint (`ontology.py:1714-1736`),
NL2SQL planning (`nl_answer_service.py:739`). **Subagents/delegation receive NO
BCS block** (delegate_tool.py:967-968 — verified; matches the no-parent-context
invariant). org_id always derived from verified rows (conversation org / KB row),
never client input; department is context-only everywhere.

**Confirmed defects:**
- **F1 — Phase 3B datasource narrowing is DEAD CODE in production.**
  `nl_answer_service.py:739-740` calls `build_ontology_planning_context`
  WITHOUT `datasource_id`, so `if ds_ids and datasource_id` never fires —
  scoped SQL rules flow into planning for every datasource. The kb_id is in
  scope at :736. One-line fix + test.
- **F2 — BCS chat payload ignores `datasource_ids` narrowing**
  (`_rule_candidates` :280-310 filters only org/deleted/status) — a rule scoped
  to datasource A enters prompts for datasource-B conversations (guidance-level
  leak). Fix: narrow scoped rules against the calling agent's bound KBs when
  `agent_id` resolves (narrow-only, never widen; org-wide rules always pass;
  behavior preserved when agent unresolvable).
- **F3 — rejected processes enter the BCS payload** (`_process_candidates`
  :178-181 has no status filter) — violates invariant 8 (they're labeled but
  injected). Fix: exclude `evidence_status == 'rejected'`.
- **F4 — BCS metric candidates lack the planning gates** (`_metric_candidates`
  :316-327 — no is_enabled/lifecycle filter; planning requires enabled+published
  :635-637). Align.
- **F5 (deferred):** planning path has no token budget (rules/links unbounded)
  and KG business-context (`knowledge_graph/business_context.py`) duplicates
  BCS with looser rules (exposes metric SQL in prompts) — consolidation
  candidate for a future phase, gated by KG_BUSINESS_CONTEXT_ENABLED.

**NOT defects (verified):** rejected links/rules/metrics excluded everywhere
that matters (7B); token budget enforced on the payload item stream; per-section
fail-open never fabricates content.

---

## Recommended changes (prioritized)

| # | Task | Sev | Files |
|---|------|-----|-------|
| T1 | B1 pinned-datasource org wall | critical | data_source_runtime.py (FD) |
| T2 | B2 workspace-settings authz | high | workspace_settings.py (clean) |
| T3 | B3 auto-bind doc fix | low | data_source_runtime.py (FD) |
| T4 | D0 cleanup dead-wiring fix | critical | cleanup.py (clean) |
| T5 | D2 automation delete cleanup | high | automation_api.py (clean) |
| T6 | D3 bulk delete cleanup | high | entity_service.py + entities.py (FD) |
| T7 | D4 AgentApp binding cleanup | med | cleanup.py + entities.py (FD) |
| T8 | D1 watchdog missing-resource tombstone | high | drift_watchdog.py (clean) |
| T9 | D6 onboarding digest org filter | low | onboarding.py (clean) |
| T10 | D5 list_links is_deleted filter | med | queries.py (clean) |
| T11 | E1 canonical provenance ungate | high | nl_answer_service.py (FD) |
| T12 | E2 honest evidence_status | med | provenance.py (clean) |
| T13 | C1 nl2sql audit org stamp | high | nl2sql/__init__.py (FD) |
| T14 | C2 audit registry reconciliation | med | audit_event.py (clean) |
| T15 | A1 marker-prefix guard | med | knowledge_bases.py (clean) |
| T16 | F1 pass datasource_id to planning | high | nl_answer_service.py (FD) |
| T17 | F2 BCS rule narrowing via agent bindings | med | business_context.py (clean) |
| T18 | F3 rejected processes excluded | med | business_context.py (clean) |
| T19 | F4 BCS metric gates aligned | low | business_context.py (clean) |

FD = foreign-dirty (surgical staging required).

## Changes that should NOT be made
- Remove SemanticGuidelines (it is the NL2SQL retrieval layer for rules; only
  the bridge owns derived rows and tests pin manual-row safety).
- Invent a Datasource deletion API (none exists; honestly documented).
- Remove `auto_bind_all_datasources` outright (default OFF, admin-gated after
  T2; removal needs user migration — deprecated, not removed).
- Change v2 pinned-datasource behavior (B4) or KG business-context (F5) —
  documented deferred.
- Force-enumerate all event types through a new audit service abstraction
  (16 call sites; two failure-safe helpers already exist — vocabulary
  reconciliation + contract test is the honest fix).
