# Zhanlu Gap Analysis — 14 Sources (IBM × 8, Palantir × 6) → Concrete Improvements

Date: 2026-10-15  
Companion to: `2026-10-15-zhanlu-business-value-strategy.md` (strategy) — this doc is the *gap → solution* layer.  
Method: all 14 articles fetched and read in full (2026-10-15); every claim below cross-checked against the actual backend (`backend/app`).

---

## 1. Reality Check: what Zhanlu ALREADY has (do not rebuild any of this)

| Capability per articles                                                     | Zhanlu status             | Evidence (file)                                                                                                                       |
| --------------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| ROI / cost measurement ("95% of GenAI pilots fail — nobody measures value") | ✅ Done 2026-10-13         | `services/roi_service.py` (IBM Gap 1), `services/llm_cost_recorder.py`                                                                |
| Use-case portfolio / AI-CoE board                                           | ✅ Done                    | `services/usecase_portfolio.py`, `routers/portfolio.py` (IBM Gap 2)                                                                   |
| In-product AI literacy (starter prompts, prompt coach)                      | ✅ Done                    | `services/ai_literacy.py` (IBM Gap 4)                                                                                                 |
| Vertical domain packs                                                       | ⚠️ Partial — only 2 packs | `services/domain_packs.py` (IBM Gap 5); `backend/domain_packs/insurance_claims.json`, `manufacturing_c5.json`                         |
| Governance: approvals, policy decisions, audit                              | ✅ Exists                  | `routers/governance.py` (policy-decisions, approvals approve/reject), `models/audit_event.py`                                         |
| Business glossary + semantic metrics for NL2SQL                             | ✅ Exists                  | `routers/terminologies.py` (VERIFIED glossary), `models/metric_definition.py`                                                         |
| Auto entity/schema discovery (ontology raw material)                        | ✅ Exists, schema-level    | `services/knowledge_graph/` — entity_extractor, schema_linker, domain_discovery, join_edge_detector, metric_bootstrap, workload_miner |
| Entity/link models                                                          | ⚠️ Exists, project-scoped | `models/knowledge_catalog.py`: `ProjectEntity`, `ProjectEntityLink`                                                                   |
| Agent evals + observability traces                                          | ✅ Exists                  | `routers/admin_evals.py`, `routers/admin_observability_traces.py`, `eval_harness.py`                                                  |
| Access policies / org model                                                 | ✅ Exists                  | `routers/access_policies.py`, `models/organization.py`                                                                                |
| Multi-agent orchestration, planning FSM, reflexion, self-heal, fallback     | ✅ Closed (2026-07 report) | `services/swarm/`, `synexia/FSM`, `tool_retry.py`, `provider_fallback.py`                                                             |
| Enterprise orchestrator                                                     | ✅ Exists                  | `services/enterprise_orchestrator/` (profiler, executor, claim_tracker, synthesizer)                                                  |

**Reframe of the problem:** Zhanlu is NOT missing the parts. It is missing the **unifying layer that makes the parts one system** — exactly Palantir's point: *"The Ontology is not a semantic layer … it is a multimodal system."* Zhanlu owns semantic fragments (glossary, metrics, entities, approvals, packs) that don't yet compose into a fourfold (data + logic + action + security) operating layer, and the admin cannot SEE any of it as "how Zhanlu supports my enterprise."

---

## 2. Real Enterprise Scenario: one day at a petrochemical trading company (customer #1 = your own org)

Walk the chain, then mark where it works today:

```
08:30  Price-intelligence agent detects C5 spot price jump (market_prices + news Signal)
  → 09:00  Procurement agent proposes: pull forward purchase from Supplier A, drafts PO
  → 09:20  Procurement lead APPROVES the PO draft (human confirmation, audit-logged)
  → 09:30  PO writes back to ERP (Alibaba Cloud RDS)
  → 10:00  Sales agent answers customer "what's your current C9 quote?" — with live ERP data + confidence
  → 11:00  Price adjustment proposal routed to sales director for approval
  → 14:00  News Signal hits Supplier B (port closure) → risk alert fans out to procurement + supply chain agents
  → 17:00  Weekly China market report auto-generated (prices, supply-demand signals, next-week advice) and dispatched
```

| Link                                                     | Status today                   | Blocking gap                                                                       |
| -------------------------------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------- |
| Detect price jump + generate alert                       | ✅ works (EDIA + market_prices) | —                                                                                  |
| Agent proposes procurement action                        | ⚠️ agent can *say* it          | No ontology Action object bound to a tool with risk tier                           |
| Human approval flow                                      | ⚠️ infra exists, not wired     | `governance.py` approvals not connected to agent actions                           |
| PO write-back to ERP                                     | ❌ missing                      | No write-path tool for ERP objects                                                 |
| Sales NL query with confidence                           | ⚠️ nl2sql exists               | agent-builder streaming bug breaks UX; no citation/confidence surface in this flow |
| Price-change approval routing                            | ❌ missing                      | same as approval gap                                                               |
| Cross-domain signal fan-out (news → supplier → 2 agents) | ⚠️ swarm exists                | No ontology links (Signal→Supplier) to route on                                    |
| Weekly report end-to-end                                 | ⚠️ EDIA generates              | dispatch + ontology aggregation (Report object) incomplete                         |

**8 links, 2 fully working.** Every broken link traces to the same root: **no unified ontology object/action layer to bind agents, tools, approvals, and audit to business semantics.**

---

## 3. Article-by-Article Gap Tables

### 3.1 IBM sources

**① enterprise-ai** — prescriptions: measurable business value, governance, integration, reliability, scalability, security; agentic AI as multi-step reasoners under oversight; RAG on proprietary data.

- Zhanlu: value measurement ✅ (roi_service); governance pieces ✅ (governance.py, access_policies.py); agentic loop ✅ (FSM/swarm/reflexion)
- **Gap G1 — value/governance not surfaced per business domain.** ROI is per-agent/per-org, not per-domain ("what does AI return in my procurement department?").
- **Solution:** add `domain_id` to the ROI/portfolio rollups; coverage_state aggregates per-domain value metrics (schema in strategy doc §8.4). Effort: S.

**② chatbot-use-cases** — prescriptions: chatbot→assistant→agent continuum; escalation to humans with full context; employee-facing internal bots (AskHR 11.5M interactions).

- Zhanlu: chat stack ✅ (chat_sessions, SSE, FSM)
- **Gap G2 — no escalation-with-context pattern.** When an agent can't resolve, there is no structured handoff (conversation summary + collected data → human queue).
- **Solution:** add an `escalation` action type to ontology actions → creates governance approval request with full context manifest (reuse `context_manifest.py`). Effort: M.

**③ ai-agents-in-marketing** — prescriptions: content generation agents, campaign orchestration, social listening, trends agent, personalization.

- Zhanlu: no marketing pack.
- **Gap G3 — no Marketing domain pack.** Structural-only per strategy §8.2.
- **Solution:** `domain_packs/marketing.json` starter ontology (Campaign/Content/Audience/Channel + verbs). Effort: S. Note: weekly-report content is already a marketing-adjacent asset.

**④ ai-agents-in-procurement** — prescriptions: supplier selection/evaluation, contract management, PO automation, demand forecasting, compliance monitoring, inventory, order readiness; agents negotiate/reroute with real-time data.

- Zhanlu: forecasting ✅ (EDIA), nl2sql ✅
- **Gap G4 — the verbs are missing.** No Supplier/PO/Contract objects with bound actions (PO draft, risk scan, alternate sourcing), no write-back.
- **Solution:** procurement is the FIRST deep pack: implement ontology_action rows + tools + approval wiring (see backlog P0). Effort: L.

**⑤ ai-agents-in-sales** — prescriptions: lead scoring, CRM-integrated engagement, forecasting, sales coaching, nurture.

- Zhanlu: ERP Sales agent ✅ (RDS + ask_data_agent)
- **Gap G5 — agent-builder streaming bug degrades the flagship demo** (`_execute_tool_healed` → `execute_tool(caller_conversation_id=...)` TypeError; Postgres tools table missing columns is_default/skill_md/sources/references; frontend /api proxy 3001→5002). Also known: schema migration debt.
- **Solution:** fix stream path + migration + proxy (precondition for everything, backlog P0-1). Effort: S/M.

**⑥ ai-agents-supply-chain** — prescriptions: continuous sensing → decision → action within guardrails; cross-function context sharing (planning/procurement/manufacturing/logistics); autonomous actions inside defined limits; 61% revenue-growth delta stat.

- Zhanlu: swarm ✅, FSM ✅
- **Gap G6 — no cross-domain event routing on ontology links.** Swarm agents coordinate but not via business semantics (Signal→Supplier→Contract→PO).
- **Solution:** ontology link traversal in `enterprise_orchestrator`: when an object changes (new Signal row), fan out to agents bound to linked objects' domains. Effort: M.

**⑦ ai-customer-service-chatbots** — prescriptions: 24/7, instant response, escalation with context, omnichannel voice consistency, KPI monitoring (resolution rate, CSAT).

- Zhanlu: chat infra ✅, answer_feedback ✅
- **Gap G7 — no service KPI instrumentation per bot.** answer_feedback exists but no resolution-rate/CSAT rollup per agent on the coverage map.
- **Solution:** feed answer_feedback + agent_invocations into coverage_state metric scores. Effort: S.

**⑧ ai-in-ecommerce** — prescriptions: order intelligence (orchestration, demand forecasting), dynamic PXM, payments/fraud, TRUST as the critical success factor (14% consumers satisfied; bad bots lose customers forever).

- Zhanlu: trading platform ✅ (orders, prices)
- **Gap G8 — trust surfacing is thin.** Confidence badges designed in EDIA but not standardized platform-wide; no per-answer citation/provenance display contract.
- **Solution:** standardize a `TrustPanel` contract (citations + confidence + data freshness from `freshness.py`) in every artifact/chat answer. Effort: M.

### 3.2 Palantir sources

**⑨ ontology-system** — prescriptions: fourfold data+logic+action+security; nouns AND verbs; decision graph; language/engine/toolchain; security reconciled per interaction across humans AND agents.

- Zhanlu: fragments of all four, **no composition.** ProjectEntity/Link (nouns, no actions), terminologies/metrics (logic fragments), governance approvals (action infra), access_policies (security).
- **Gap G9 — THE core gap: no Ontology core that composes objects + actions + logic + security and that agents operate through.**
- **Solution:** new `services/ontology/` composing existing fragments: ontology_object/link/action tables (strategy §8.4) with `ProjectEntity`/`Terminology`/`MetricDefinition` as backing sources; action rows bind `tool_id` + `risk_level` + `approval_policy`. Agents query ontology, not raw SQL. Effort: L (P0/P1 spine).

**⑩ platforms/ (AIP+Foundry+Apollo = Enterprise OS; 9 capability sets)** — prescriptions: ontology language/engine/toolchain + data/logic/workflow services + analytics/automations/delivery, on shared security/governance mesh.

- **Gap G10 — no single admin surface where the enterprise SEES the system.** Portfolio = agent-level; datasources = table-level; nothing at domain level.
- **Solution:** Admin "Enterprise Profile" page: Coverage Map (14 domains × readiness) + Ontology Graph Explorer (reuse knowledge_graph graph rendering + `ProjectEntity` nodes). Effort: M/L.


**⑪ multimodal-data-plane ("any data, any compute, any model, anywhere")** — prescriptions: open formats, no data duplication (virtual tables), BYO compute/model, uniform governance over all of it.

- Zhanlu: multi-DB connectors ✅, model abstraction/fallback ✅, doc ingestion ✅
- **Gap G11 — multimodal I/O is the declared main upgrade direction but not landed** (input: voice/image/doc; output: rich artifacts).
- **Solution:** keep as its own track (already planned); ontology layer must be modality-agnostic from day one (properties_json allows typed media). Effort: L (separate track).

**⑫ interoperability** — prescriptions: REST/JDBC/S3 openness, metadata + semantic interop, Palantir MCP for agent-driven semantic access, webhooks for write-back.

- Zhanlu: MCP server support ✅ (`routers/mcp.py`), integrations ✅, webhook-ish hooks ✅
- **Gap G12 — no ontology-level external API.** Third-party tools can't ask "what objects/actions does this org expose?"
- **Solution:** expose `/api/ontology/{org}/objects|actions|links` REST (auth-scoped) — also the future MCP tool surface. Effort: S/M.

**⑬ rubix/ (ephemeral, zero-trust substrate; blue/green via Apollo)** — prescriptions: workload isolation, enforced cycling, zero-downtime upgrades.

- Zhanlu: docker-compose deploy, sandbox exists (`routers/sandbox.py`, `Dockerfile.sandbox`)
- **Gap G13 — no upgrade/rollback story; single-instance mindset.** Acceptable for mid-market private deploy; do not clone Rubix.
- **Solution:** minimal: versioned DB migrations + blue/green via docker-compose override + health-gated restart script. Effort: S. Defer anything more.

**⑭ aip-architecture (12 capability categories)** — prescriptions: secure LLM integration, end-to-end observability incl. tokens, context engineering, agent lifecycle with Evals, human+AI on same foundation, package/release/deploy, enterprise automation agents.

- Zhanlu: evals ✅, traces ✅, cost/token ledger ✅, agent studio ✅
- **Gap G14 — agent release lifecycle incomplete:** evals exist but are not a gate in a promote-to-production path; no packaging of ontology+agents+data bindings as a deployable "template."
- **Solution:** wire eval_harness into a `promote` action on agent_lifecycle (eval pass ≥ threshold required); template export = domain pack + bound agents + ontology subset (the P2 "petrochemical template" deliverable). Effort: M.

---

## 4. Consolidated Improvement Backlog (priority order)

| #    | Item                                                                                                                                                                               | Closes gaps | Effort | Why now                                                               |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- | ------ | --------------------------------------------------------------------- |
| P0-1 | Fix agent-builder streaming bug: `caller_conversation_id` TypeError + Postgres schema migration (tools: is_default, skill_md, sources, references) + frontend /api proxy 3001→5002 | G5          | S/M    | Flagship demo is broken; blocks all dogfooding                        |
| P0-2 | Ontology core service `services/ontology/`: object/link/action tables + composition of ProjectEntity/Terminology/MetricDefinition; agents read via ontology                        | G9, G4      | L      | The spine every other item attaches to                                |
| P0-3 | Wire governance approvals into ontology actions (risk_level: read=auto, write=approval; audit_event on every action)                                                               | G2, G4, G9  | M      | Turns "chatting database" into operating system; infra already exists |
| P0-4 | Procurement deep pack: Supplier/PO/Contract objects + PO-draft + supplier-risk tools + ERP write-back path                                                                         | G4          | L      | First real business loop = the value proof                            |
| P1-1 | Admin Enterprise Profile: Coverage Map (14 domains) + Ontology Graph Explorer                                                                                                      | G10         | M/L    | "Whole enterprise, end-to-end" becomes visible to the buyer           |
| P1-2 | 14 starter domain packs (one JSON each, from IBM map)                                                                                                                              | G3 + all    | S×14   | Structural whole-enterprise coverage, cheap                           |
| P1-3 | Cross-domain event fan-out in enterprise_orchestrator on ontology links                                                                                                            | G6          | M      | Decision graph = compounding moat                                     |
| P1-4 | Per-domain ROI/metric rollups into coverage_state                                                                                                                                  | G1, G7      | S      | Value story per department                                            |
| P1-5 | TrustPanel standard (citations + confidence + freshness) platform-wide                                                                                                             | G8          | M      | IBM: trust is the #1 success factor                                   |
| P1-6 | Eval-gated promote in agent lifecycle + template export (pack+agents+ontology)                                                                                                     | G14         | M      | Makes P2 template business possible                                   |
| P2-1 | Ontology REST/MCP external surface                                                                                                                                                 | G12         | S/M    | Ecosystem + integrations                                              |
| P2-2 | Minimal blue/green deploy + versioned migrations                                                                                                                                   | G13         | S      | Deployment credibility                                                |
| P2-3 | Multimodal I/O track (input/output)                                                                                                                                                | G11         | L      | Already the declared main upgrade direction; runs in parallel         |

**Sequencing rule:** P0-1 → P0-2 → (P0-3 ∥ P0-4) → P1. P2-3 runs as an independent track throughout.

---

## 5. What NOT to build (anti-goals, confirmed by the articles)

- Do NOT clone Rubix/Apollo-grade infrastructure — mid-market private deploy doesn't need FedRAMP-grade ephemerality (Gap G13 minimal answer only)
- Do NOT build a new eval/observability/orchestration layer — three of each already exist; the work is composition and surfacing
- Do NOT hand-draw customer ontologies — knowledge_graph's discovery machinery must generate the draft (strategy §9); a consulting-project onboarding kills the mid-market play
- Do NOT do all 14 domains deeply — depth follows the customer's data; procurement first because the ERP connection and price data already exist
