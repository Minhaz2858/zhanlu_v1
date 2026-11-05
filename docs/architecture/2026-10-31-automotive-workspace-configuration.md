# Zhanlu — Automotive Enterprise Workspace Configuration (Implementation Report)

2026-10-31 · Zero LLM credits consumed · **No commits made** · Target stack:
`zhanlu-postgres` (:5400) / `zhanlu-backend` (:5002) — the stack where the
existing manufacturing agents and department projects live.

## 1. Departments (8/8 present in My Space)

| Department | Project | Action |
|---|---|---|
| Manufacturing & Production | existing `ba061d89…` | REUSED (dept=manufacturing, description aligned) |
| Supply Chain & Procurement | existing "Procurement & Supplier Management" `0d701688…` | REUSED + renamed; KB legacy name-string kept consistent |
| Quality Management & Assurance | existing `e33af4a5…` | REUSED |
| Sales & Dealer Management | existing `5f543a98…` | REUSED |
| Aftersales & Customer Service | — | CREATED |
| R&D & Vehicle Engineering | existing "Research & Development (R&D)" `fa34d70a…` | REUSED + renamed |
| Finance & Cost Management | — | CREATED |
| Logistics & Distribution | — | CREATED |

Pre-existing non-department projects (ACME ERP, Data analysis, test,
Executive Management & Strategy) untouched.

## 2. Existing agents reused and reassigned (9 — IDs, prompts, KBs, conversations preserved)

Production Scheduling Agent → Manufacturing (profile enriched: output-decline /
bottleneck diagnostics described; capability tags added) · Predictive
Maintenance Agent → Manufacturing · Supply Chain Optimization Agent +
Inventory Management Agent → Supply Chain & Procurement · Quality Control
Agent → Quality · Demand Forecasting Agent → Sales · Autonomous Vehicle
Systems Agent + CAD Agent (published for discoverability) → R&D · Customer
Support Agent → Aftersales (reassigned from the archived "Data analysis"
personal project; configured with full automotive prompt layers, governed
toolset, low-risk policy, published).

## 3. New agents created (5 — genuine gaps, no duplicates)

Supplier Risk Agent (supply chain) · Dealer Performance Agent (sales) ·
Warranty & Service Agent (aftersales) · Cost Analysis Agent (finance) ·
Logistics & Distribution Agent (logistics). All: five prompt layers with an
explicit RESOURCE-HONESTY boundary (bound sources only; never fabricate),
governed read/analysis toolset (NL2SQL, ontology, artifacts, escalation,
peer delegation — no write verbs), low risk tier, status=published,
company resource type.

## 4. Database changes

- **Migrations 113–118 applied** (the DB sat at alembic 112 while the mounted
  code required the chain): purely additive — new tables
  (`project_knowledge_bases`, ERP outcome-measurement, enterprise-process/review
  lifecycle) plus `business_processes` columns. Old running backend unaffected.
- **Department discovery SQL fixed**: `_list_available_agents`' department
  batch had a cross-dialect `CAST(p.department AS VARCHAR) = ANY(ARRAY[...])`
  that Postgres rejects (ANY(array) ≠ =ANY(ARRAY[...])); replaced with an
  engine-portable `IN (...)` — discovered live during Phase E verification.
- New department projects + agents via the REUSE-FIRST idempotent seed
  `scripts/configure_automotive_workspace.py` (dry-run supported; re-runnable).

## 5. UI changes

None required — My Space project cards, tabs and agent management already
render from the Project/AgentApp records (department label on agent rows,
agent counts by `project_id`). Frontend production build exit 0.

## 6. Verification (Phase D/E, all credit-free)

- `scripts/verify_automotive_workspace.py` — **ALL CHECKS PASSED**:
  the REAL `list_available_agents` handler returns 23 published agents with
  correct department + home project + data domains; department-scoped
  discovery contains only supply_chain (+ unscoped); semantic selection with
  the LOCAL MiniLM embedder routes 5/5 clear-domain automotive objectives to
  the right specialist, "hi" to no one, and never capability-stretches
  ("translate to french", "weather" → no match).
- Focused regression: 83 passed (matcher/planner/delegation suites).
- Existing conversations, resources, permissions: untouched (rebinding is
  `project_id` only).

## 7. Known limitations

1. **No real automotive systems are connected.** Departments and specialists
   are configured; their answers are grounded only in whatever data sources
   are actually bound (currently the procurement warehouse KB and sales KBs).
   This is a workspace CONFIGURATION, not automotive data integration.
2. The semantic matcher's reviewed honesty threshold (0.35) routes two
   borderline analytic phrasings ("why did output drop", measured 0.325;
   "declining profitability", 0.264) to GA direct execution rather than a
   specialist — honest (the GA's LLM still sees the full discovery list), but
   measurable.
3. Directory hygiene: `每周销售报告工作流` is a published *workflow* with a
   Chinese description and no department; its degenerate embedding scores
   0.341 against unrelated English prompts — just under the threshold.
   Recommend the Owner either give it an English profile description or
   exclude workflow-as-agent entries from the specialist directory.
4. Unscoped (department-less) agents appear in department-scoped discovery —
   pre-existing design; an Owner decision is needed if they should be hidden.
5. Project cards do not display model selection (existing UI; not redesigned
   per mission). Recent Chats: existing conversations left untouched; new
   chats created from a department project inherit its `project_id` via the
   existing flow.

## 8. Rollback

Re-run nothing destructive is needed: new projects/agents are plain rows
(soft-delete them); renamed projects + KB name-strings can be restored to
"Procurement & Supplier Management" / "Research & Development (R&D)"; agent
`project_id` values are recorded in the seed script. Migrations 113–118 are
additive and safe to leave in place.
