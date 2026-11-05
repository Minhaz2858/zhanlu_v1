# Zhanlu — Automotive Enterprise Agent Coverage Matrix

**Date:** 2026-10-31 · Companion to `zhanlu-current-enterprise-agent-architecture-audit.md` (evidence base). Read-only analysis; no implementation proposed beyond prioritization.

**Status vocabulary:**
- **IMPLEMENTED** — working end-to-end functionality today.
- **PARTIAL** — functionality exists but important capabilities are missing.
- **REGISTERED ONLY** — agent exists in the marketplace with real prompts/tools but cannot complete a department task end-to-end (no data bound, not published).
- **MISSING** — no corresponding agent exists.
- **SHARED CAPABILITY** — provided by a reusable platform service rather than an independent agent.

**Platform services available to EVERY department agent (do not rebuild per department):** `ask_data_agent` NL2SQL pipeline with grounding/execution gates · document RAG (hybrid retrieval) · ontology read (`query_ontology`) + governed write actions · approval/HITL loop (paused/resume, PO drafts, escalations) · artifact/report generation · dashboards (`create_fullstack_dashboard`, `universal_kpi/trend`) · web search · scheduling/cron (automations) · delegation (`delegate_task`, with the 4 breakpoints documented in Doc A §10) · multi-tenant RLS isolation.

---

## 1. The 14 Departments

### 1. Executive Management
- **Responsibilities:** cross-functional oversight, KPI review, profitability and strategy decisions.
- **Workflows:** monthly business review, escalation triage, what-if portfolio analysis.
- **Typical systems:** ERP (executive views), BI/data warehouse, EPM.
- **Key entities:** company KPIs, profit centers, org structure, board reports.
- **Zhanlu support:** **SHARED CAPABILITY** — `general_assistant` + dashboard/KPI tools + automation reports; no dedicated Executive Briefing agent.
- **Missing:** cross-department orchestration (Doc A §10 breakpoints), KPI dictionary binding to ontology `metric` backing (G5).
- **Collaboration:** consumer of every other department's outputs.

### 2. R&D and Engineering
- **Responsibilities:** vehicle/part design, CAD, BOM, change management, validation.
- **Workflows:** part design → DFM check → quote/BOM → revision release.
- **Typical systems:** PLM, CAD (Fusion/CATIA/NX), PDM, simulation.
- **Key entities:** parts, revisions, BOM lines, drawings, change requests.
- **Zhanlu support:** **PARTIAL — strongest existing vertical**: CAD Agent (live Fusion 360 toolset, parametric models, drawings, export), CAD DFM rules (CNC/sheet-metal), deterministic quoting, `CadRevision` with approval-gated release, BOM generation with procurement handoff, 3D viewer (P0–P4 all shipped). PLM integration and change-impact analysis beyond CAD are missing.
- **Collaboration:** BOM → Procurement (quote drafts) already wired via `create_quote_draft`.

### 3. Manufacturing and Production
- **Responsibilities:** production execution, scheduling, line performance, downtime.
- **Typical systems:** MES, SCADA historians, APS, ERP PP module.
- **Key entities:** work orders, routings, work centers, shifts, OEE, downtime reasons.
- **Zhanlu support:** **REGISTERED ONLY** — Production Scheduling Agent (real prompts/tools incl. `delegate_task`; no MES/production DB bound, not published). Shared: `universal_trend/kpi`, automations, dashboards.
- **Missing:** MES/SCADA connectivity patterns, OEE metric definitions, schedule-write-back (should stay approval-gated).

### 4. Quality Management
- **Responsibilities:** incoming/in-process/final inspection, SPC, 8D/NCR/CAPA, supplier quality.
- **Typical systems:** QMS, SPC software, LIMS.
- **Key entities:** inspection lots, defects, control charts, CAPA actions.
- **Zhanlu support:** **REGISTERED ONLY** — Quality Control Agent (SPC via code execution on fetched rows, root-cause tracing, vision for part images). Shared: document RAG for quality standards.
- **Missing:** QMS integration, structured 8D workflow, image-dataset pipelines.

### 5. Supply Chain and Logistics
- **Responsibilities:** planning, warehousing, transport, supplier risk.
- **Typical systems:** WMS, TMS, SCM/APAS, ERP MM.
- **Key entities:** warehouses, stock, shipments, routes, supplier risk.
- **Zhanlu support:** **REGISTERED ONLY** — Supply Chain Optimization Agent (supplier scorecards, PO/quote drafts, invoice matching). Inventory agent (below) covers stock positioning.
- **Missing:** WMS/TMS connectivity, transport-cost optimization, multi-echelon planning.

### 6. Procurement and Supplier Management
- **Responsibilities:** sourcing, PO lifecycle, supplier performance, contracts.
- **Typical systems:** ERP MM/PUR, SRM, e-sourcing.
- **Key entities:** suppliers, POs, receipts, contracts, price lists.
- **Zhanlu support:** **PARTIAL (deepest tooling)** — approval-gated `create_po_draft` / `create_quote_draft` / `match_invoice` (2/3-way match), supplier scorecard analysis, ontology Contract/Supplier objects with governed actions, escalation queue. Contract-expiry sensing exists via the generic sensor pipeline.
- **Missing:** supplier portal integration, e-auction, contract authoring.

### 7. Sales and Dealer Management
- **Responsibilities:** order-to-delivery, dealer network, pipeline, incentives.
- **Typical systems:** CRM, DMS (dealer), CPQ.
- **Key entities:** customers, leads, orders, dealers, incentives.
- **Zhanlu support:** **PARTIAL (shared tools, no dedicated agent)** — `score_leads` (grounded, honest about unmapped fields), `segment_records` (read-only GROUP BY), ontology Customer objects + bindings, template import for CRM-like domains.
- **Missing:** dedicated Sales/Dealer agent; DMS/CRM connector patterns.

### 8. Marketing
- **Responsibilities:** campaigns, lead generation, brand analytics.
- **Typical systems:** CRM marketing module, web analytics, CDP.
- **Zhanlu support:** **PARTIAL** — Market Intelligence Scout (industry signals), `plan_campaign` (objective→plan, read-tier; outbound stays human-executed), web search/extract.
- **Missing:** campaign execution integrations, attribution analytics.

### 9. After-Sales Service
- **Responsibilities:** warranty, claims, service parts, customer support.
- **Typical systems:** CRM service, warranty systems, parts catalog (EPC).
- **Zhanlu support:** **PARTIAL/MISSING** — a seeded generic `Customer Support Agent` exists (skills-bound), RAG covers manual/knowledge answering; **no warranty/claims/service-parts agent**. Telemetry analysis capability exists in the Autonomous Vehicle Systems agent (fleet data) which overlaps here.
- **Missing:** claims workflow, parts-availability linkage to Inventory agent, dealer service analytics.

### 10. Finance and Accounting
- **Responsibilities:** controlling, product costing, AR/AP, reporting.
- **Typical systems:** ERP FI/CO, consolidation, tax.
- **Key entities:** cost centers, GL entries, invoices, margins.
- **Zhanlu support:** **PARTIAL** — Financial Report Generator (charts/reports from bound data), `match_invoice`, Demand Forecasting (financial planning input), figure-grounding guardrail makes finance answers honest.
- **Missing:** dedicated Finance Controller agent; cost-object margin models; consolidation logic.

### 11. Human Resources
- **Responsibilities:** staffing, shifts, training, payroll interfaces.
- **Typical systems:** HCM, workforce management.
- **Zhanlu support:** **MISSING** (only Office Document Generator for HR docs). Deliberate: low agent value until demand exists.

### 12. IT and Digital Transformation
- **Responsibilities:** data platform, integrations, agent governance.
- **Typical systems:** data warehouse, iPAM/ITSM, integration middleware.
- **Zhanlu support:** **PARTIAL (platform itself is the product)** — Data Pipeline Builder (flow design), agent_builder meta-agent, skill marketplace, AdminConsole (escalations, ontology, observability), blue/green deploy script, external API surface. 
- **Missing:** org-scoped skill namespaces, ITSM connectors.

### 13. Maintenance and Facilities
- **Responsibilities:** equipment maintenance, spares, facility ops.
- **Typical systems:** CMMS/EAM, SCADA historians.
- **Zhanlu support:** **REGISTERED ONLY** — Predictive Maintenance Agent (telemetry profiling, risk ranking, maintenance windows; spares link to Inventory). CMMS integration missing.
- **Missing:** CMMS work-order write-back (should be approval-gated ontology action), vibration-spectrum analytics.

### 14. Environment, Health, Safety & Compliance
- **Responsibilities:** incidents, audits, permits, ESG reporting.
- **Typical systems:** EHS suites, audit tools.
- **Zhanlu support:** **PARTIAL** — Ops Compliance Monitor (audit reports), ontology sensing (threshold → notify → agent fan-out), escalation queue, audit_events trail. EHS-specific incident management missing.

---

## 2. Matrix

| # | Department | Agent | Existing Status | Existing Implementation | Missing Capabilities | Priority |
|---|---|---|---|---|---|---|
| 1 | Executive Mgmt | Executive Briefing | SHARED CAPABILITY | general_assistant + dashboards + automations | cross-dept orchestration; KPI ontology backing (G5) | P2 |
| 2 | R&D / Engineering | CAD Agent | **IMPLEMENTED** | Fusion 360 toolchain, DFM, quote, BOM, revisions, 3D viewer | PLM integration; change-impact beyond CAD | P3 |
| 3 | Manufacturing / Production | Production Scheduling Agent | REGISTERED ONLY | real 5-layer prompts, 21 real tools, `delegate_task` | data bindings; publish; MES patterns; schedule write-back (gated) | **P1** |
| 4 | Quality Management | Quality Control Agent | REGISTERED ONLY | SPC via code exec, vision, root-cause prompts | data bindings; publish; QMS/8D workflow | **P1** |
| 5 | Supply Chain / Logistics | Supply Chain Optimization Agent | REGISTERED ONLY | scorecards, PO/quote drafts, invoice matching | data bindings; publish; WMS/TMS patterns | **P1** |
| 6 | Procurement | (same agent as #5) | PARTIAL | po_draft/quote_draft/match_invoice + ontology Contract actions + escalation | supplier portal; contract authoring | P2 |
| 7 | Sales / Dealer | Sales Agent | MISSING | shared tools: score_leads, segment_records, ontology Customer | dedicated agent + CRM/DMS bindings | P2 |
| 8 | Marketing | Market Intelligence Scout | PARTIAL | industry signal agent + plan_campaign (read-tier) | attribution; execution integrations | P3 |
| 9 | After-Sales | Warranty & Service Agent | MISSING | Customer Support Agent (generic RAG); AV telemetry overlap | claims workflow; parts linkage | P2 |
| 10 | Finance | Finance Controller Agent | MISSING (tools PARTIAL) | Financial Report Generator + match_invoice + forecasting | dedicated agent; cost-object models | **P1** |
| 11 | HR | HR Agent | MISSING | — | — | P3 (demand-driven) |
| 12 | IT / DT | Data Pipeline Builder + platform | PARTIAL | pipeline builder, admin console, governance | org-scoped skills; ITSM | P2 |
| 13 | Maintenance / Facilities | Predictive Maintenance Agent | REGISTERED ONLY | telemetry profiling, risk ranking, windows | data bindings; publish; CMMS write-back (gated) | **P1** |
| 14 | EHS / Compliance | Ops Compliance Monitor | PARTIAL | audit reports + sensing + escalation queue | EHS incident workflow | P3 |

**Anti-duplication rule (enforced by this matrix):** Procurement shares the Supply Chain agent; Marketing's campaign needs are read-tier capabilities, not an agent; Executive Management is a consumer scenario, not an agent until P2; HR/EHS agents are created only when a department's data is actually connected.

---

## 3. Business data requirements per priority department (P1)

| Department agent | Minimum bound tables/concepts | Systems of record | Ontology objects |
|---|---|---|---|
| Production Scheduling | work orders, routings, work-center calendar, BOM, inventory on-hand | ERP PP / MES | WorkOrder, WorkCenter, BOM, Product |
| Quality Control | inspection results, defect log, batch/lot, supplier lot | QMS / MES | Batch, Defect, Inspection, SupplierLot |
| Supply Chain Optimization | suppliers, POs, receipts, lead times, prices, contracts | ERP MM | Supplier, PurchaseOrder, Contract, Part |
| Predictive Maintenance | asset registry, sensor telemetry, maintenance history | CMMS / historian | Equipment, MaintenanceOrder, SensorReading |
| Demand Forecasting | sales/order history by SKU/model, promotions calendar | ERP SD / DW | Product, SalesOrder, Forecast |
| Finance Controller (new) | GL/cost centers, invoices, margins by model | ERP FI/CO | CostCenter, Invoice, Profitability |

For each: connect via the existing KnowledgeBase datasource flow (`source_kind='database'` → catalog index → semantic enrichment → `nl2sql_ready`), map objects in the ontology (discovery proposes, human binds to `verified`), and bind per agent via `AgentApp.knowledge_bases`.

---

## 4. Coverage verdict

**Today an automotive manufacturer can get real value from:** CAD/DFM/quote/BOM (Engineering), governed procurement drafts + invoice matching (Procurement), data-grounded Q&A/reporting in ANY department the moment its database is connected (the shared NL2SQL pipeline is department-agnostic), compliance reporting, and market intelligence.

**It cannot yet get:** department agents that answer from the company's own systems out of the box (all 7 manufacturing agents are unbound shells), cross-department investigations (Doc A §10 breakpoints), or warranty/finance/sales department agents (missing).

**The bottleneck is not agent count — it is (1) data binding per deployment, (2) the 4 collaboration breakpoints, (3) three missing department agents (Sales, Finance, After-Sales).**
