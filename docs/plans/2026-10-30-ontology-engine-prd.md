# PRD — Zhanlu Enterprise Ontology: Gap-Closure Program

**Date:** 2026-10-30 · **Status:** draft for review
**Basis:** `docs/audits/2026-10-30-ontology-gap-audit.md` (evidence-backed gap audit G1–G8)
**Stance:** extend the existing `services/ontology/` layer. No new subsystem, no graph database (relational + JSON proven), no rebuild of discovery/actions/governance.

**Non-negotiables carried over from the proposal (already house rules):** no hardcoded enterprise ontology; no single-database assumption; no fabricated relationships (evidence or explicitly inferred); LLMs propose, deterministic code validates/computes; no permission bypass; no auto-publish of uncertain models; continuous evolution.

---

## Phase 0 · Governance hardening (G7 + G3-bug) — small, do first

**Verified execution-path map** (audited 2026-10-30 — every path funnels through exactly two service functions):

| Caller | Path | Context available |
|---|---|---|
| Agent tool | `ontology_tool.py:_execute_ontology_action` → `execute_action(..., context=ctx)` | `agent_name` present from the agent loop (`tool_executor.py:127`) — but **`ontology_tool.py:29` defaults a missing agent_name to `"general_assistant"`**: a masquerade hazard the checker must NOT inherit |
| HTTP (any user) | `routers/ontology.py:184` → `execute_action` | only `user_id` — must be marked `caller_kind="console_user"` |
| HTTP approved | `routers/ontology.py:604` → `execute_approved_action` | same marking required |
| MCP / external | tool registry (same handlers) → `execute_action` | context may be empty → **unidentified → denied for writes** |
| Nested bound tool | `data.py` `execute_tool(..., context={...**, (context)})` | caller context propagates automatically |

**Scope — fail closed, centrally enforced**
1. New `services/ontology/permissions.py::check_action_permission(db, org_id, action, context)` — the single decision point, called from BOTH `execute_action` (before approval creation) and `execute_approved_action` (before the tool runs). Rules:
   - **Write action** (`risk_level="write"` or `requires_confirmation`):
     - Agent caller with **no binding** on the action's domain → **denied** (fail closed).
     - Agent caller with only `read`-scope binding(s) → denied.
     - Multiple bindings: any `write`-scope row on that domain grants; read-only rows never do.
     - **Unidentified caller** (no `agent_name`, no `caller_kind`) → denied. The checker uses the raw `context.agent_name` — never the `"general_assistant"` default.
     - `caller_kind="console_user"` (authenticated human via HTTP) → allowed to *request*; the governance approval flow still gates the actual run (unchanged human semantics).
   - **Read action**: unchanged (no binding check — `read=auto` is the existing risk contract).
   - **Approval does not override scope**: a read-only agent is denied at `execute_approved_action` even with a valid, matching, unexpired approval.
2. Denials write a structured `AuditEvent` (`event_type="policy_violation"`, `event_source="ontology"`, `outcome="denied"`, reason + caller identity).
3. Whitelist the vocabulary: add `ontology` to `AUDIT_EVENT_SOURCES`; add `ontology_fanout`, `escalation_created`, `escalation_resolved` to `AUDIT_EVENT_TYPES` (`models/audit_event.py:16-22`). No migration (string columns).

**Acceptance**
- No binding → write denied, **no ApprovalRequest is created**; read-only binding → denied; write binding → approval created (normal flow).
- Router paths marked console-user keep today's human behavior (request → approval → execute-approved) with no binding requirement.
- Empty-context (MCP) write → denied; empty-context read → works.
- `execute_approved_action` re-checks scope: read-scope agent + valid approval → denied.
- Every denial audited and visible in the governance audit list.
- Full existing ontology suite stays green (no breaking change to reads).

**Security implications (explicit):** MCP write execution breaks until the MCP caller is identified with a binding — acceptable and intended (writes were never legitimately available to unauthenticated callers; the approval gate previously masked this). Reads intentionally stay open to preserve the `read=auto` contract.

---

## Phase 1 · Unified evidence taxonomy (G3)

**Scope**
1. Migration: add to `ontology_objects`, `ontology_links`, `ontology_actions`:
   - `evidence_status` ∈ `verified | computed | inferred | proposed | unavailable` (default `proposed` for new rows; existing seeded packs = `proposed`; human-bound = `verified`)
   - `confidence` FLOAT NULL (LLM/scored elements only — never fabricated for human-confirmed rows)
   - `provenance_json` (source refs: kb_id/table/columns, document_id+page, approving user, method)
2. Single source of truth module `services/ontology/evidence.py`: the five statuses + mapping rules from existing vocabularies (discovery `bound→verified/proposed`, coverage readiness stays derived, TrustPanel grades remain answer-level and map at the boundary).
3. `serialize.py` emits the new fields; discovery writes confidence+method into `provenance_json` instead of transient report only; `PATCH /bind` sets `verified` + records the admin's identity.
4. Frontend: replace the ad-hoc chips with one `EvidenceBadge` (EnterpriseProfile + any future surface).

**Acceptance**
- Round-trip: discover (inferred/proposed) → admin binds (verified, provenance records who) → serialize shows both states.
- No vocabulary is invented at render time; all five statuses have one definition site.
- TrustPanel behavior unchanged (answer-level grades untouched).

---

## Phase 2 · Versioned ontology registry (G4)

**Scope**
1. Migration: `ontology_revisions` table (org-scoped): `entity_type`, `entity_id`, `revision_no`, `change_kind` (create/update/bind/retire), `before_json`/`after_json` (diff), `changed_by`, `approval_id` NULL (link to governance ApprovalRequest when the change is a write-tier ontology edit).
2. **Transactional revisions (non-negotiable):** the ontology mutation and its revision row commit in the SAME database transaction — `record_revision` participates in the caller's session/transaction, never a separate commit. Consequences verified by tests:
   - Concurrent updates: revision numbering guarded by the DB (per-entity `revision_no` allocated inside the same transaction; unique constraint `(entity_type, entity_id, revision_no)` — a losing writer rolls back with its mutation, no torn state).
   - Failed revisions: if the revision insert fails, the mutation rolls back with it (ontology state must always be reconstructible from history).
   - Rollback: session rollback discards mutation + revision together (test with a forced exception mid-transaction).
   - Import/export: template import records create-revisions for every imported element; export is read-only (no revisions).
   - Retired objects/links/actions: soft-delete is a mutation → retire-revision; history survives retirement.
3. All mutations of ontology elements flow through the `record_revision` hook (discovery autobind, PATCH bind, import, retire, future phases) — no direct writes bypass it. Idempotent re-runs (discovery, import) that change nothing record **zero** revisions.
4. Endpoints: `GET /ontology/{entity_type}/{id}/revisions` (diff list), `GET /ontology/revisions?since=` (feed). Optional: require an approval for *manual* edits of `verified` elements (config flag, default off).
5. Bump `template_version` to reflect content hash instead of the hard-coded `1.0.0`.

**Acceptance**
- Every ontology mutation produces exactly one revision row with a readable diff, in the same transaction (forced-failure test proves mutation+revision roll back together).
- Re-running idempotent discovery or import produces **zero** new revisions.
- Concurrent same-entity mutations never produce duplicate/gapped revision numbers.
- Revision history renders per object in the UI (simple list, no fancy diff viewer needed for v1).

---

## Phase 3 · Cross-source identity resolution (G1) — the centerpiece

**P3a prerequisite — structured row access for spreadsheets (verified gap).** Uploaded Excel/CSV today become *text chunks only* (`document_ingestion/extractors.py:81-93` converts sheets to text for RAG; no row store exists). Identity resolution needs queryable records. P3a adds: on ingestion of xlsx/csv (and on demand), parse sheets deterministically (openpyxl — already a dependency) into a queryable staging table (`file_rows`: kb_id, sheet, row_idx, values_json) with column-type inference — bounded row cap. Database-backed sources already have row access via `QueryService`. **Until P3a ships, P3 scope is databases only; spreadsheets join when P3a lands.**

**Identity matching — four strictly separated tiers.** The PRD's earlier "1001 ≡ C1001" example under *exact* matching was wrong: those strings are not equal. Tiers, never conflated:

1. **Exact identifier matching** — raw values equal after trivial, lossless normalization only (trim, case-fold, type coercion int↔string). `1001 ≡ 1001`. High confidence, auto-link as `confirmed` (still reversible via review queue).
2. **Evidence-backed deterministic normalization** — a transformation (`C1001 → 1001`, prefix stripping, zero-padding) is applied ONLY when justified by evidence recorded in the match rule: e.g. value-set containment across the two columns (`join_edge_detector` VALUE_OVERLAP shows the transformed sets coincide), or an admin-declared identifier rule per source (stored in provenance with author + date). **Never strip prefixes or transform identifiers on assumption**; an unmatched transformation never fires.
3. **Cross-source semantic matching** — name/attribute similarity (embedding + field-level agreement across multiple attributes, not name alone) proposes candidates; always `status=inferred`, always human-reviewed.
4. **Probabilistic matching** — low-confidence or single-evidence candidates land in the review queue with the evidence displayed. Confidence is surfaced, never auto-acted on.

**Scope**
1. Migration: `identity_matches` (org-scoped): `object_id`, `source_a` (kb_id+table/sheet+pk), `source_b`, `match_tier` (1-4 above), `normalization_rule_json` NULL (tier 2 only — the exact evidence-backed transform), `confidence`, `status` ∈ `inferred | confirmed | rejected`, `evidence_json` (sampled co-occurrence, attribute agreements), `reviewed_by/at`.
2. Tier 1+2 run deterministically over candidate column pairs from `join_edge_detector` blocking (read-only, sampled, capped). Tier 2 rules must cite their evidence and are re-validated against live samples before each run.
3. Tier 3+4: LLM/embedding proposals land in the review queue only. Everything human-rejected is remembered: the same method never re-proposes the same pair.
4. **Never merge rows.** The deliverable is *crosswalk links* (source A pk ↔ source B pk per object), consumed by `read_object_data` as optional cross-source enrichment — an object read can note "same customer appears in CRM as C1001 (confirmed)".
5. Endpoints: `POST /ontology/identity/resolve` (run tiers for an object), `GET /ontology/identity/matches?status=`, `POST /ontology/identity/matches/{id}/confirm|reject` (org-admin, audited, revision-recorded per Phase 2).
6. UI: "Identity review" section in EnterpriseProfile (queue with side-by-side sample rows, confirm/reject, the evidence + normalization rule shown for tier-2 matches).

**Acceptance**
- Two-source fixture (MySQL customer table + ingested Excel sheet with `C1001` style ids): tier 1 links exact-equal ids without LLM; `1001` vs `C1001` is NOT linked by tier 1 — it links only via an evidence-backed tier-2 rule whose proof is visible, or waits in review.
- A deliberately ambiguous pair (similar names, disjoint ids, no normalization rule) lands in the review queue, never auto-linked.
- Rejected matches are never re-proposed by the same method.
- No record copying: crosswalk only; underlying tables untouched.

---

## Phase 4 · Document-grounded ontology proposals (G2)

**Scope**
1. Widen discovery candidates: include text KBs (`source_kind != "database"`) as evidence inputs — document chunks (already chunked+embedded by upload_rag) are retrieved per domain/object and used as *evidence*, not as bindings.
2. New `services/ontology/document_grounding.py`: for a chosen object/domain, retrieve top chunks, one budgeted LLM call proposes: candidate properties (with quoted evidence span + doc ref), candidate rules/constraints ("approval rule", status `proposed`, provenance = document evidence), candidate links ("Policy governs PurchaseOrder"). Proposals are written as ontology elements with `evidence_status=proposed` — never auto-activated.
3. **Evidence provenance is exact and complete.** Every proposal's `provenance_json` records ALL of: document ID, page number or sheet name, chunk ID, the exact source text span (verbatim quote), extraction method (model/prompt version), and document version/hash at extraction time. A policy document can suggest a business rule, but the rule remains `proposed` until authorized human confirmation — and re-runs against a changed document version produce NEW proposals, never silent updates of old ones.
3. Rules proposed from documents land in the existing SemanticGuideline staging pattern (disabled until human-enabled), consistent with `business_rule_extractor.py`.
4. UI: per-object "Suggest from documents" action; proposal cards with quoted evidence (all provenance fields visible).

**Acceptance**
- Upload a procurement policy PDF → propose "PO > ¥10k requires manager approval" as a proposed rule whose provenance is the document + quote; nothing activates without admin confirmation.
- LLM quotes must be verifiable: proposal rejected if the quoted span is not found in the source chunk (deterministic validation, no fabricated evidence).

---

## Phase 5 · Activate metric/entity backings (G5)

**Scope**
1. Implement `backing_kind="metric"` reads in `data.py`: resolve the referenced `MetricDefinition` and execute it **through the existing enforcement chain — the MetricDefinition validation (`nl_answer_service.py:1085-1101` binding-constraint injection) and the NL2SQL pre-execution gates (`query_contract.py:61-87` + `execution_gate.py:342-383`)** — returning the scalar/series with formula provenance. **No new SQL execution mechanism**: the ontology layer resolves WHICH metric, never runs ad-hoc SQL itself; datasource authorization and the read-only gate remain exactly where they are today. Remove the "not yet supported" stub only when the metric executes through that path.
2. `backing_kind="entity"`: implement ONLY if the Phase 3 identity crosswalk gives it a concrete use (an object whose rows resolve across sources via confirmed crosswalks); otherwise **remove `entity` from `BACKING_KINDS`** (no dead vocabulary). Decision point: end of Phase 3 review.
3. Bridge registries: ontology object property ↔ `MetricDefinition` id; add `owner` + `lineage_json` to MetricDefinition (migration); `ProjectMetric` approvals can promote into `MetricDefinition` (one path, human-gated).
4. Domain-value hard-coded computations (`domain_value.py`) get registered as derived metrics with `evidence_status=computed` where it pays off (report recipes can then reuse them); pure-UI rollups may stay in code.

**Acceptance**
- An object property backed by a metric returns a grounded value with formula provenance; the NL2SQL execution gate still enforces the binding (no parallel computation path).
- No ontology element claims `metric` backing without a resolvable MetricDefinition (validation at bind time).

---

## Phase 6 · Evolution actuation (G6)

**Scope**
1. **Detect actual changes — never schedule blind discovery.** The drift runner compares the cached `schema_snapshot` vs `refresh_schema()` per KB (reuse `datasources/drift.py detect()`) and classifies: removed columns, changed column types, changed table structure (added/dropped tables), new resources, stale bindings, changed document versions (hash). Only elements whose underlying evidence actually changed are reconsidered; unchanged elements produce **zero** revisions and zero noise.
2. On drift: validate only the ontology bindings of the affected KB — mapped columns still exist, types still compatible? → mark affected properties `evidence_status=unavailable` (never delete), raise a fan-out notification, record a revision (Phase 2 transactional hook).
3. New-KB hook: after catalog reindex (`routers/knowledge_bases.py:268-352`), enqueue a **proposal-only** discovery run for the NEW resource only (autobind stays admin-invoked by default; flag `ONTOLOGY_DISCOVERY_AUTOBIND_ON_KB_ADD`, default false). Existing bindings are untouched.
4. Sensor breaches may attach structured `proposal` payloads to the fan-out (candidates for new sensors/thresholds), still never mutating the ontology without a human or a revision record.
5. Binding-health view in EnterpriseProfile (stale bindings surfaced with "re-discover" action).

**Acceptance**
- Drop a mapped column in a test DB → within one drift cycle, that property is `unavailable`, exactly one revision exists, agents on the domain were notified — and **no other object's bindings or revisions changed**; re-adding the column + re-discover restores it.
- A no-op drift check (nothing changed) produces zero revisions, zero notifications.
- Connecting a new KB produces discovery *proposals* for that KB without admin action, but changes nothing bound without admin confirmation.

---

## Phase 7 · Ontology explorer UI (G8)

**Scope**
1. `GET /ontology/graph` (objects+links+evidence status, org-scoped; reuse existing list services).
2. New tab in EnterpriseProfile: interactive link graph (lazy-loaded D3 or Mermaid; follow the Model3DViewer lazy-chunk pattern), node = object (color = evidence status), edge = link type; click → object drawer (properties, provenance, revisions, identity crosswalks, backing health).
3. Data-source mapping view: for each object, the exact kb/table/columns/documents that support it (mostly existing data, newly visible).

**Acceptance**
- Graph renders for the seeded 14-domain org without perf issues at ~50 objects/50 links; every node/edge state is traceable to evidence fields.

---

## Sequencing & sizing

| Phase | Gap | Size (rough) | Dependency |
|---|---|---|---|
| P0 Governance hardening | G7, G3-bug | S (≤1 day) | — |
| P1 Evidence taxonomy | G3 | M | — |
| P2 Revision registry | G4 | M | P1 (fields to diff) |
| P3a Structured row ingestion (xlsx/csv) | P3 prerequisite | S-M | — |
| P3 Identity resolution | G1 | L | P0-P2 complete, P3a for spreadsheets |
| P4 Document grounding | G2 | M-L | P1 |
| P5 Metric backings | G5 | M | P1; entity decision after P3 |
| P6 Evolution actuation | G6 | M | P1, P2 |
| P7 Explorer UI | G8 | M | P1 (badges), benefits from P2/P3 |

P0→P2 form one coherent batch (honesty + governance substrate) — **P3 does not start before P0-P2 are complete**: identity resolution needs trustworthy evidence, provenance and revision history. P3a can run in parallel with P1/P2. P3 and P4 are independent of each other. Every phase ships with tests following the existing suite patterns (59 ontology tests today) and the ≤~100-line file discipline for new service modules.

**Shared-foundation principle (governs every phase):** the enterprise ontology is the shared, governed semantic foundation for Zhanlu's agents, reports, analytics and workflows. Every capability here is exposed through the existing integration surfaces (ontology tools, routers, fan-out, TrustPanel, external API) — never built as an isolated feature with its own private access path.

## Explicit non-goals (this program)

- Graph database / Cypher (relational + JSON suffices at current and foreseeable scale)
- Row-level materialization or golden-record *merging* (crosswalk links only; source systems stay authoritative)
- Rewriting discovery, actions, sensing, or the governance approval flow (they work)
- Auto-publishing any LLM-proposed ontology element without human confirmation
