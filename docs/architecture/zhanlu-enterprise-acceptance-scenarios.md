# Zhanlu Enterprise Acceptance Scenarios — Automobile Manufacturing Domain

> **ARCHITECTURE CORRECTION (2026-11-01): Projects, not Departments.**
> Organization provides tenant isolation; **Projects provide the business
> context and resource boundary**; agents are REUSABLE capabilities that may
> serve multiple Projects when authorized (`ProjectAgent` join table). The
> five validation contexts below are therefore PROJECTS — the Automobile
> Sales Intelligence, Manufacturing Operations, Supply Chain Intelligence,
> Quality Intelligence, and Executive Business Intelligence projects — not
> organizational departments. Capability matching ranks by relevance;
> PROJECT AVAILABILITY + publication status + authorization decide what may
> be selected. The legacy `department` label in the codebase is discovery
> metadata only (see audit §Architecture) and gates nothing new.

**Date:** 2026-11-01 · Companion to `zhanlu-enterprise-reliability-audit.md`.
**Purpose:** formal deterministic specification of acceptance scenarios A–E. This is the contract the Phase-5 acceptance suite (`backend/tests/test_enterprise_acceptance.py`) implements. It builds on the EXISTING deterministic pipeline pattern (`tests/test_manufacturing_acceptance.py`: `build_turn_plan` → `semantic_select_specialists` → `evaluate_observation`/`decision_from_review` → `classify_failure` → `validate_query_result`) and the existing golden fixtures (`tests/test_manufacturing_agent_golden.py` Auto Sales Sandbox reference figures). **No competing framework is created. No paid LLM calls.**

---

## 0. Routing rule (applies to every scenario)

The correct number of agents depends on the task. The routing ladder (deterministic, implemented by `turn_planner.build_turn_plan` + GA tool-choice + `delegate_task`):

| Input shape | Execution strategy | Agents invoked |
|---|---|---|
| Simple greeting / meta question | `direct` (no tools) | GA only, zero tool calls |
| Simple data retrieval (one bounded question) | `direct_tool` — GA + governed NL2SQL (`ask_data_agent`/`query_database`) | GA only |
| Complex single-project analysis | `single_specialist` — GA delegates to one specialist | GA + 1 specialist |
| Cross-project analysis | `multi_agent` / `plan_dag` — GA coordinates, heterogenous batch or DAG nodes | GA + N specialists (N = projects/specialties actually implicated) |
| High-risk / destructive / write | human approval gate (`paused` → approval → resume); never auto-executed | unchanged |

Anti-pattern (asserted NEGATIVE in the suite): delegation of greetings, delegation of simple lookups, or fanning out to every specialist for a single-domain question. **No artificial agent activity.**

## 1. Fixture design (deterministic, zero credits)

- **Demo DB:** SQLite in-memory automobile-industry schema (sales_order, vehicle_model, region, customer, supplier, purchase_order, inventory, production_line, work_order, defect_record, warranty_claim) with seeded rows whose expected aggregates are pre-computed constants in the fixture module (same pattern as `REF` in the golden suite). RLS-exempt app-layer org filters apply as in production single-tenant.
- **LLM boundary:** the deterministic pipeline functions (`build_turn_plan`, `semantic_select_specialists(embedder=_fixture_embedder)`, `evaluate_observation`, `classify_failure`, `resume_decision`) need NO LLM. Where a real orchestration function requires an LLM (tool-choice, SQL generation), the suite uses the existing mock/stub seams — planner fixtures and, where a v3-pipeline test is practical, a monkeypatched chat-completion returning a scripted response. No live provider.
- **Failure injection:** scripted exception payloads (timeout, connection refused, permission denied) into `classify_failure`/healing-budget paths, as in the existing acceptance suite.
- **Scenario A–E fixtures reuse the Auto Sales Sandbox reference figures where they overlap (sales).**

## 2. Scenario A — Sales intelligence

- **User utterance:** "Analyze last week's automobile sales performance. Identify the fastest-growing vehicle models, regional performance, customer concentration, and revenue changes."
- **Expected intent:** complex single-project analysis (Sales Intelligence project) → `single_specialist` (GA → Demand Forecasting / sales specialist) **or** GA + governed NL2SQL when the data answers it directly; both are acceptable iff exactly one specialist is selected and no unnecessary cross-project fan-out occurs.
- **Checks:**
  1. Intent/strategy ∈ {single_specialist, direct_tool}; NOT multi_agent.
  2. Capability selection returns the sales-domain specialist top-ranked (deterministic embedder); no quality/procurement specialist selected.
  3. Delegation (when taken): child runs with published AgentApp identity; toolset = child's `tool_config`; parent KB context inherited (post-P1 flag flip).
  4. Data retrieval: SQL is read-only, passes the execution gate; date window is last-week (explicit concrete dates, per `task_spec_parser` rewrite rule); aggregates match fixture constants exactly (revenue sum, top-growing model, regional split, top-customer share).
  5. Evidence: successful data tool result registered as `EvidenceItem` (`turn_evidence`), ids resolvable; no number appears in the summary that is not in tool results (grounding check semantics).
  6. Reviewer: verdict `complete`; a missing-evidence review yields `delegate_specialist`/`needs_revision`, never silent pass.
  7. Missing data: a region absent from fixtures is disclosed as unavailable — never reported as zero without a source.
  8. Persistence: one authoritative assistant row (`answer_persisted=True` on done); execution tree shows 1 supervisor + ≤1 specialist node with terminal status.

## 3. Scenario B — Supplier risk intelligence

- **User utterance:** "Identify suppliers that may disrupt our next month's vehicle production. Analyze delivery delays, supplier concentration, material availability, and procurement exposure."
- **Expected intent:** cross-specialty within the Supply Chain Intelligence project (procurement + inventory) → `multi_agent` with exactly the implicated specialties (≤3 specialists: Supply Chain, Inventory; Demand Forecasting only if production volumes are needed).
- **Checks:**
  1. Strategy ∈ {multi_agent, plan_dag}; specialist set ⊆ {supply_chain, inventory, demand_forecasting}; no quality/HR-style agents invoked.
  2. Data retrieval: delivery-delay rates, supplier spend concentration (HHI-style share), material coverage vs next-month requirement — each number traced to a fixture constant; measured facts labeled as measured.
  3. Risk scoring: any score derived from retrieved data (grounded); predictions (disruption likelihood) presented as predictions with their inputs listed — measured vs predicted distinction asserted on the final output structure.
  4. Failure path: injected warehouse timeout → `classify_failure` → environmental → bounded retry within `HealingBudget`; recovery does not duplicate completed nodes.
  5. Mitigation actions: recommendations only; any PO-draft write requires the approval gate (asserted: draft action not auto-executed; approval-requested observation yields `paused` semantics, never silent write).
  6. Persistence + tree: specialist nodes terminal; partial completion (if a branch fails after budget) represented honestly as partial, not failed-success.

## 4. Scenario C — Production and inventory intelligence

- **User utterance:** "Which production lines are at risk of missing next week's production targets because of inventory shortages or equipment problems?"
- **Expected intent:** cross-specialty within the Manufacturing Operations project (production + inventory, + predictive maintenance when equipment signals exist) → `multi_agent` / DAG with production + inventory (+ maintenance) specialists.
- **Checks:**
  1. Specialist selection picks production_agent top; inventory_agent second; maintenance agent included only when equipment evidence is implicated (fixture: a line with a maintenance flag).
  2. Cross-specialty evidence reconciliation: affected-line determination combines production target vs inventory on-hand vs open maintenance orders; contradictions (e.g., inventory says fine, open maintenance order says line down) are surfaced, not averaged away.
  3. No double counting: a line's risk is reported once even when two specialists contribute evidence.
  4. Zero vs missing: a line with no inventory rows at all is "data unavailable", not zero risk/zero stock, unless a row states zero on-hand.
  5. Recommended actions cite evidence ids; verification gate passes only with data grounding (failed-step + grounded ⇒ pass-with-failed-steps listed).
  6. Tree + persistence per §2.8.

## 5. Scenario D — Quality intelligence

- **User utterance:** "Analyze recent quality defects and warranty claims. Identify recurring defect patterns, affected vehicle models, and possible supplier-related causes."
- **Expected intent:** single-project (Quality Intelligence) with optional supply-chain collaboration → `single_specialist` (Quality Control) expanding to include Supply Chain only when supplier-linked lots are found (fixture: one defect lot traces to a supplier).
- **Checks:**
  1. Strategy ∈ {single_specialist, multi_agent}; if multi, second specialist is supply_chain (not arbitrary).
  2. Defect trend: recurrence counts match fixture constants (per model, per defect code); correlation (defect lot ↔ supplier lot) asserted only where the fixture joins actually connect — otherwise explicitly uncertain.
  3. Unproven root causes: language/structure must distinguish observed correlation from proven cause (no "root cause" claim without a joined supplier-lot record).
  4. Evidence + provenance per §2.5; reviewer verdict per §2.6.
  5. Warranty window: date boundary = recent claims window with explicit dates (no all-time scan).

## 6. Scenario E — Executive decision intelligence

- **User utterance:** "Prepare an executive report covering sales, production, procurement, inventory, and quality risks for the last month."
- **Expected intent:** executive report across the five projects → GA coordinates `multi_agent`/`plan_dag` with one specialist per implicated project, then consolidates and exports.
- **Checks:**
  1. Plan covers exactly the 5 requested domains; no extra specialists/projects; each node independent with appropriate boundaries.
  2. Consolidation: each domain's KPIs appear once (no double counting); contradictions between domain findings are surfaced in the report (asserted on report structure: a contradictions/notes section exists when fixture injects one).
  3. Missing data: one department's fixture window is empty → report discloses "no data for <domain> in window" rather than zero-fill; the deterministic synthesizer derives KPIs only from returned rows (`_report_auto_analysis` semantics).
  4. Export: report artifact created (`finalize_into_artifact` semantics: Artifact + ArtifactVersion + file_exports list); HTML export deterministic; a PPTX/DOCX export path is exercised via the sandbox/document-generator seam with mocked external boundary where needed.
  5. Grounding + verification per §2; persistence: artifact row + message link + one authoritative assistant message.

## 7. Per-scenario invariant checklist (identical across A–E)

Every scenario test asserts, where applicable:

| # | Invariant | Source of truth |
|---|---|---|
| I-1 | Correct intent + execution strategy (routing ladder §0) | `build_turn_plan` |
| I-2 | Correct capability selection (right dept, no extras) | `semantic_select_specialists` |
| I-3 | Delegation only when required; right agent identity + toolset | `delegate_tool` AgentApp path |
| I-4 | Tool permissions: read-only respected; writes approval-gated | `PermissionChecker` + risk tier |
| I-5 | Data retrieval: gate-passing read-only SQL, exact fixture aggregates | `QueryService` + `execution_gate` |
| I-6 | Evidence registered, ids resolvable, no ungrounded figures | `turn_evidence` + `answer_grounding` semantics |
| I-7 | Reviewer: honest verdict; needs-evidence never silently passes | `decision_from_review` |
| I-8 | Recovery: bounded, classified, no duplicate work, honest partials | `classify_failure` + `HealingBudget` + `resume_decision` |
| I-9 | Final output: executive summary, measured-vs-predicted split, disclosures | scenario checks |
| I-10 | Execution tree: turn-scoped, correct identities/statuses/counts | `AgentExecutionTree` contract (frontend tests where practical) |
| I-11 | Persistence: one authoritative assistant message; `answer_persisted` | D4 contract (`test_fsm_message_identity`) |

## 8. Machine-readable acceptance report

The suite emits `backend/reports/enterprise-acceptance-report.json`: `[{scenario_id, expected, actual, passed, evidence, failure_category, implementation_location}]`. Failure categories: `routing`, `capability_selection`, `delegation`, `permissions`, `data`, `evidence`, `verification`, `recovery`, `output`, `tree`, `persistence`. The human-readable summary is `docs/architecture/zhanlu-enterprise-acceptance-report.md`. **No live-provider validation is claimed anywhere in the report.**
