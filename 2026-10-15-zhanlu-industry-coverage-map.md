# Zhanlu × IBM AI-Agent Use Cases — Industry Coverage Map (2026-10-15)

Sources: all 8 IBM Think articles (enterprise-ai, chatbot-use-cases, ai-agents-in-marketing,
ai-agents-in-procurement, ai-agents-in-sales, ai-agents-supply-chain, ai-customer-service-chatbots,
ai-in-ecommerce) — fetched and read in full. Companion to `2026-10-15-zhanlu-gap-analysis-14-sources.md`
(Palantir architecture gaps — closed 2026-10-15) and `2026-10-15-zhanlu-business-value-strategy.md`.

Question answered here: **what do AI agents actually do in each enterprise function/industry,
can Zhanlu do it today, and what do we update to serve every enterprise?**

Legend: ✅ = live today · ⚠️ = partial (components exist, needs wiring) · ✗ = missing · ➖ = deliberate non-goal

---

## 1. Supply Chain (ai-agents-supply-chain)

| IBM use case | Zhanlu today | Update needed |
|---|---|---|
| Continuous forecast updating from real-time signals | ✅ ForecastRun + MAE/RMSE accuracy tracking; forecast dashboard | ⚠️ ingest external signals (news/weather/social) into sensing |
| Dynamic plan adjustment | ⚠️ agents propose via artifacts; plans not yet executable | P3-1: forecast→replan action verb (approval-gated) |
| Inventory flow management / rebalancing | ✅ Inventory object bound to real tables; rebalance = write action (approval-gated) | — |
| Supplier performance monitoring | ✅ Supplier→live ERP tables; read actions; domain value rollups | — |
| Alternative supplier suggestion / disruption flags | ⚠️ ontology links (Supplier→PO) + fan-out exists | P3-2: sensing automations that trigger fan-out on schedule |
| Production scheduling optimization | ➖ manufacturing_c5 pack provides vocabulary; scheduling engines out of scope | — |
| Shipment/carrier monitoring, dynamic rerouting | ⚠️ Order/Shipment objects readable; no external carrier feeds | P3-2 (same sensing pipeline) |
| Risk: continuous scanning, impact assessment, pre-escalation | ⚠️ web_search tool exists but not wired into scheduled sensing | P3-2 |
| **Autonomous action within guardrails** | ✅ risk-tiered actions; writes always approval-gated (governance) | — |
| **Cross-function context sharing in real time** | ✅ ontology-link fan-out (`notify_object_change`) + swarm mailboxes | P3-3: fan-out on data-change detection, not just manual calls |
| Decisions at the point of impact, not dashboards | ✅ agents act through ontology actions inside workflows | — |

**Verdict:** strongest coverage. To finish: scheduled sensing → fan-out → mitigation proposals.

## 2. Customer Service (ai-customer-service-chatbots)

| IBM use case | Zhanlu today | Update needed |
|---|---|---|
| FAQs from a knowledge base | ✅ RAG + agentic retrieval (hybrid, grading, decomposition) | — |
| Order tracking & status | ✅ `query_ontology`/`read_object_data` on bound Order tables | — |
| Appointment scheduling & reminders | ✗ no scheduling verb | P3-4: `schedule_event` action type (read tier) |
| Technical troubleshooting step-by-step | ✅ RAG + tool loop | — |
| Account self-service (updates, refunds) | ✅ write actions, approval-gated | — |
| Lead qualification & routing | ⚠️ CRM/sales pack exists | P3-5: lead-scoring tool + Lead object verb |
| AskHR (employee self-service) | ✅ hr domain pack + PII-safe read tiers | — |
| **Escalation with full context transfer** | ⚠️ handoff is conversational only | P3-6: `escalate` action type that packages summary+data+trace into a ticket |
| Sentiment-based escalation | ✗ | P3-6b: sentiment scorer on inbound messages → escalate verb |
| KPIs: resolution rate, CSAT | ⚠️ ExperienceEntry + answer_feedback exist | P3-7: roll resolution/CSAT into per-domain value (extend P1-4) |
| Multilingual | ✅ (language layer exists) | — |
| Proactive support (detect issues before reported) | ⚠️ automations + sensing (P3-2 shared) | — |

## 3. Procurement (ai-agents-in-procurement)

| IBM use case | Zhanlu today | Update needed |
|---|---|---|
| Supplier discovery & evaluation | ✅ LIVE: Supplier bound to real ERP, read actions, comparisons | — |
| PO automation | ✅ LIVE: `create_po_draft` write-back, approval-gated, xlsx export | — |
| Contract management & obligations | ⚠️ document_workflow pack; no contract object verb | P3-8: contract object + expiry-alert sensing |
| Compliance checks pre-commitment | ✅ governance approvals + audit trail | — |
| Supplier risk monitoring | ⚠️ P3-2 sensing | — |
| Invoice matching (3-way) | ✗ | P3-9: `match_invoice` tool (read-heavy, low risk) |
| Spend analytics | ✅ read actions + BI-style artifacts (charts) | — |

## 4. Sales (ai-agents-in-sales)

| IBM use case | Zhanlu today | Update needed |
|---|---|---|
| Lead scoring & prioritization | ✗ | P3-5 (shared with CS) |
| CRM hygiene & data entry | ⚠️ write actions can update CRM tables | P3-10: `update_crm` convenience verb (approval-gated write) |
| Personalized outreach drafting | ✅ generation + artifacts | — |
| Forecasting | ✅ forecast module w/ accuracy tracking | — |
| Pipeline analysis | ✅ read actions on Order/Contract tables + chart artifacts | — |
| Sales coaching | ✗ | P3-11: post-call analysis skill (transcript → feedback) |
| Quotes/CPQ | ⚠️ PO-draft machinery reusable | P3-12: quote object + draft verb |

## 5. Marketing (ai-agents-in-marketing)

| IBM use case | Zhanlu today | Update needed |
|---|---|---|
| Content generation (drafts, variants) | ✅ generation + artifacts + packs | — |
| Campaign orchestration | ⚠️ campaign vocabulary only | P3-13: campaign object + `launch_campaign` write verb |
| Audience segmentation | ⚠️ Customer tier/segment properties; no clustering tool | P3-14: segmentation analysis tool (SQL-driven, read tier) |
| Social listening | ✗ (no feed ingestion) | P3-2 shared (external signal ingestion) |
| Personalization at scale | ⚠️ needs segment data + templates | after P3-13/14 |
| Media buying / ad ops | ➖ deliberate non-goal (external ad platforms, spend risk) | — |
| Campaign analytics | ✅ chart artifacts over bound data | — |

## 6. E-commerce (ai-in-ecommerce)

| IBM use case | Zhanlu today | Update needed |
|---|---|---|
| Recommendations / dynamic segmentation | ✗ (ML recommender out of core) | ➖ or P3-14 extension |
| Product content automation (PXM) | ✅ artifact generation; pack vocabulary | — |
| Order orchestration & fulfillment optimization | ⚠️ read + propose; execution needs WMS integration | same P3-1 verb family |
| Fraud detection | ✗ | ➖ deliberate non-goal (needs transaction streams) |
| **Trust (transparent AI, observable interactions)** | ✅ TrustPanel (grade/confidence/citations) on every answer | — |
| Multilingual commerce content | ✅ | — |
| Payments/pricing engines | ➖ deliberate non-goal | — |

## 7. Enterprise-wide (enterprise-ai, chatbot-use-cases)

| IBM prescription | Zhanlu today |
|---|---|
| Measurable value per domain | ✅ per-domain value rollups (P1-4) |
| Governance & auditability | ✅ approvals, risk tiers, audit trail |
| Integration with existing systems | ✅ discovery v2.1 binds ANY database (LLM-matched, confidence-gated; proven on a foreign CRM) |
| Reliability (evals as release gate) | ✅ eval-gated publish + template export |
| Chatbot→assistant→agent progression | ✅ single stack, three interaction levels |
| Trust surface | ✅ TrustPanel standard |
| Domain packs speed adoption | ✅ 16 packs + starter prompts |
| Industry template packaging | ✅ template export (import side pending) |

---

## What we update next (P3 backlog, priority order)

1. **P3-2 — Scheduled sensing pipeline** (compounding; serves supply chain + procurement + CS proactive support):
   automation that runs on schedule → reads bound data + external signals (web search) → on threshold breach, calls `notify_ontology_change` → linked-domain agents propose mitigations. *This turns IBM's "continuous sense→decide→act" from manual into automatic.*
2. **P3-6 — Escalation-with-context action type** (serves CS + chatbot-use-cases #1 prescription): packages conversation summary + collected data + trace into a structured handoff; sentiment variant escalates upset customers proactively.
3. **P3-5 — Lead-scoring tool** (sales + CS lead qualification): read-tier tool scoring Lead/CRM records against criteria; results feed outreach drafting.
4. **P3-7 — Service KPIs into domain value** (CS + enterprise-ai): resolution rate + CSAT rollups next to ROI in the Enterprise Profile.
5. **P3-13/14 — Campaign + segmentation verbs** (marketing): campaign write verb + SQL-driven segmentation tool.
6. **P3-8/9/12 — Contract expiry sensing, invoice matching, quote drafts** (procurement/sales): same approval-gated verb pattern as PO drafts.
7. **Template import** (P1-6 counterpart): seed a new org from an exported bundle — makes every vertical deployable in hours.

**Deliberate non-goals** (stay honest, per gap-doc anti-goals): media buying, payments/fraud engines,
robotic warehousing, generic ML recommenders — these need transaction streams or hardware we don't
own; pretending would violate the trust contract.

**Positioning line:** IBM says every function gets a specialized agent connected through shared
context, acting inside guardrails, measured by value. Zhanlu's ontology IS that shared context,
governance IS the guardrails, domain value IS the measurement — discovery v2.1 means any company's
existing databases (ERP/CRM/PLM/anything) plug in without code. The P3 list above is what remains
to claim every use case in this document.
