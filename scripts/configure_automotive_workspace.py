#!/usr/bin/env python
"""Configure Zhanlu as an automotive enterprise AI workspace (2026-10-31).

REUSE-FIRST, IDEMPOTENT, NEVER DELETES:
  * Existing department projects are REUSED (matched by current or legacy
    name) — only description/department/resource fields are aligned.
  * Existing agents are REBOUND to their department project via the
    AgentApp.project_id FK. IDs, conversations, permissions, tool configs,
    prompt layers and KB bindings are preserved.
  * Only GENUINE gaps are created: 3 missing department projects and 5
    specialist agents that no existing agent covers.
  * Safe to re-run: every step is an upsert keyed on natural identity.

Run INSIDE the zhanlu-backend container (bind-mounts this repo and carries
the live DATABASE_URL):
    docker exec zhanlu-backend python /app/scripts/configure_automotive_workspace.py [--dry-run]

Department labels feed agent-DISCOVERY scoping ONLY (Project.department,
migration 110) — they are never an authorization mechanism. Data access
remains governed by per-child KB bindings, ResourceAccessPolicy, RLS and the
delegation broker, exactly as before.
"""
from __future__ import annotations

import sys

# ── Department specification (the approved eight) ────────────────────────────
# current_name/legacy_name → final name. `existing` projects are REUSED.
DEPARTMENTS = [
    {"name": "Manufacturing & Production", "legacy": None,
     "slug": "manufacturing",
     "description": ("Vehicle and powertrain manufacturing operations: production "
                     "scheduling, line capacity, equipment health and maintenance, "
                     "shift planning and manufacturing analytics.")},
    {"name": "Supply Chain & Procurement", "legacy": "Procurement & Supplier Management",
     "slug": "supply_chain",
     "description": ("Supplier performance, sourcing and procurement, material and "
                     "component supply risk, inventory and replenishment for vehicle "
                     "and parts programs.")},
    {"name": "Quality Management & Assurance", "legacy": None,
     "slug": "quality",
     "description": ("Vehicle quality: inspection and test results, defect detection "
                     "and classification, root-cause analysis, SPC monitoring and "
                     "quality gates across lines and suppliers.")},
    {"name": "Sales & Dealer Management", "legacy": None,
     "slug": "sales",
     "description": ("Vehicle and parts sales, dealer network performance, order "
                     "pipeline, demand forecasting and regional market analysis.")},
    {"name": "Aftersales & Customer Service", "legacy": None,
     "slug": "aftersales",
     "description": ("Vehicle owners after delivery: customer support and ticket "
                     "handling, warranty claims, service campaigns and vehicle "
                     "health history.")},
    {"name": "R&D & Vehicle Engineering", "legacy": "Research & Development (R&D)",
     "slug": "rnd",
     "description": ("Vehicle engineering programs: ADAS and telemetry validation, "
                     "sensor health, CAD design and simulation, prototype testing "
                     "and engineering change analysis.")},
    {"name": "Finance & Cost Management", "legacy": None,
     "slug": "finance",
     "description": ("Cost structure and margin analysis per vehicle model and "
                     "program, budget tracking, profitability and cost-driver "
                     "investigation.")},
    {"name": "Logistics & Distribution", "legacy": None,
     "slug": "logistics",
     "description": ("Inbound and outbound logistics: transport lanes, freight and "
                     "delivery performance, distribution network flow for finished "
                     "vehicles and parts.")},
]

# Existing agents REBOUND to their department (id/status/prompts/KBs preserved).
REBIND = [
    {"agent": "Production Scheduling Agent", "project": "Manufacturing & Production",
     # Profile enrichment (Phase E): the agent's real scope covers output
     # diagnostics, but its original description only mentioned scheduling —
     # semantic discovery (threshold-gated) could not surface it for output
     # questions. Describing the actual scope honestly; no keyword stuffing.
     "description": ("Builds and stress-tests vehicle and powertrain production "
                     "schedules against demand, capacity, material availability and "
                     "maintenance windows; diagnoses production output declines, "
                     "line bottlenecks and utilization losses for assembly lines."),
     "capabilities_extra": ["output-analysis", "throughput-diagnosis"]},
    {"agent": "Predictive Maintenance Agent", "project": "Manufacturing & Production"},
    {"agent": "Supply Chain Optimization Agent", "project": "Supply Chain & Procurement"},
    {"agent": "Inventory Management Agent", "project": "Supply Chain & Procurement"},
    {"agent": "Quality Control Agent", "project": "Quality Management & Assurance"},
    {"agent": "Demand Forecasting Agent", "project": "Sales & Dealer Management"},
    {"agent": "Autonomous Vehicle Systems Agent", "project": "R&D & Vehicle Engineering"},
    {"agent": "CAD Agent", "project": "R&D & Vehicle Engineering", "publish": True},
    {"agent": "Customer Support Agent", "project": "Aftersales & Customer Service",
     "publish": True, "configure": "customer_support"},
]

# Governed read/analysis toolset — the same set the existing manufacturing
# agents already use (governed NL2SQL, ontology reads, artifacts, escalation,
# and discovery/delegation for peer collaboration). No write verbs.
_BASE_TOOLS = ["todo", "think", "execute_code", "read_file", "write_file",
               "memory", "create_artifact", "ask_data_agent", "list_data_sources",
               "describe_schema", "execute_query", "answer_from_database",
               "query_ontology", "ontology_directory", "escalate_to_human",
               "universal_query", "universal_kpi", "universal_trend",
               "universal_forecast", "list_available_agents", "delegate_task"]

_HONESTY = ("RESOURCE HONESTY: use ONLY data sources and knowledge bases "
            "explicitly bound and authorized for this project. If no bound "
            "source can answer a question, say so plainly and suggest what "
            "to connect — NEVER fabricate, estimate silently, or invent "
            "figures.")

# GENUINE gaps — no existing agent covers these responsibilities.
NEW_AGENTS = [
    {"name": "Supplier Risk Agent", "project": "Supply Chain & Procurement",
     "description": ("Analyzes supplier delivery performance and supply risk: ranks "
                     "suppliers by late-delivery and defect exposure, examines "
                     "procurement history, evaluates material-shortage exposure for "
                     "vehicle and parts programs, and provides evidence-backed "
                     "mitigation recommendations."),
     "capabilities": ["supplier-risk", "delivery-performance", "procurement-history",
                      "shortage-exposure", "risk-mitigation"],
     "identity": ("You are Supplier Risk Agent for an automotive enterprise. You "
                  "assess supplier delivery performance, procurement history and "
                  "material-shortage exposure for vehicle and parts programs, and "
                  "rank supply risks with evidence."),
     "boundary": ("You are analysis-only: you NEVER change purchase orders, "
                  "supplier records or sourcing decisions. Risk calls must cite "
                  "the bound data behind them; missing data is stated, never "
                  "estimated."),
     "reasoning": ("Start from delivery performance and procurement history, "
                   "quantify exposure (volume at risk, single-source share), "
                   "connect findings to production impact, and propose "
                   "mitigations a buyer could approve."),
     "tools": ("Use governed NL2SQL tools (list_data_sources, describe_schema, "
               "execute_query / ask_data_agent) for procurement and supplier "
               "data, ontology tools for supplier relationships, and "
               "list_available_agents/delegate_task when a peer specialist "
               "(e.g. production or logistics) must contribute."),
     "output": ("OUTPUT: lead with the top supply risks ranked by exposure. For "
                "each: the evidence (queries run, figures, sources), affected "
                "vehicle lines/parts, and a recommended mitigation. Close with "
                "assumptions and data limitations.")},
    {"name": "Dealer Performance Agent", "project": "Sales & Dealer Management",
     "description": ("Analyzes dealer network performance: sales and order pipeline "
                     "per dealer and region, target attainment, inventory-in-trade "
                     "signals and regional market comparisons, with evidence-backed "
                     "recommendations."),
     "capabilities": ["dealer-performance", "regional-sales", "target-attainment",
                      "order-pipeline", "network-analysis"],
     "identity": ("You are Dealer Performance Agent for an automotive enterprise. "
                  "You analyze dealer sales, order pipelines and target attainment "
                  "by dealer and region."),
     "boundary": ("You are analysis-only: you NEVER modify dealer records, prices "
                  "or orders. Every figure must come from bound data; gaps are "
                  "stated."),
     "reasoning": ("Compare dealers on volume, attainment and pipeline health, "
                   "separate structural trends from one-off deals, and connect "
                   "weakness to concrete causes before recommending actions."),
     "tools": ("Use governed NL2SQL tools (list_data_sources, describe_schema, "
               "execute_query / ask_data_agent) for sales data, ontology tools "
               "for dealer/region relationships, and delegate_task for peers "
               "such as demand forecasting."),
     "output": ("OUTPUT: lead with the headline (best/worst performing dealers "
                "and regions vs target). Then the evidence tables and the "
                "reasoning behind each recommendation. Close with caveats.")},
    {"name": "Warranty & Service Agent", "project": "Aftersales & Customer Service",
     "description": ("Analyzes warranty claims and service history: claim rates and "
                     "costs by vehicle model and component, recurring failure "
                     "patterns, and connections between field failures and "
                     "production/quality evidence."),
     "capabilities": ["warranty-analysis", "claims-cost", "failure-patterns",
                      "service-history", "quality-linkage"],
     "identity": ("You are Warranty & Service Agent for an automotive enterprise. "
                  "You analyze warranty claims, service campaigns and vehicle "
                  "service history, and connect field failures to possible "
                  "production or component causes."),
     "boundary": ("You are analysis-only: you NEVER approve claims or launch "
                  "campaigns — those go to humans (escalate_to_human). Claims "
                  "data is read from bound sources only."),
     "reasoning": ("Segment claims by model, component and production period; "
                   "look for recurrence and severity patterns; when a pattern "
                   "points to production, say what evidence a quality "
                   "investigation should pull (and offer peer delegation)."),
     "tools": ("Use governed NL2SQL tools (list_data_sources, describe_schema, "
               "execute_query / ask_data_agent) for claims and service data, "
               "ontology tools for vehicle/component relationships, and "
               "delegate_task for quality or engineering peers."),
     "output": ("OUTPUT: lead with claim volume/cost trend and the top failure "
                "patterns. For each pattern: evidence, affected models/components "
                "and production periods, and the recommended next step. Close "
                "with data limitations.")},
    {"name": "Cost Analysis Agent", "project": "Finance & Cost Management",
     "description": ("Analyzes vehicle program economics: cost structure and margin "
                     "per vehicle model, cost-driver investigation, budget vs actual "
                     "variance, and cross-department profitability comparisons that "
                     "connect production, procurement and sales evidence."),
     "capabilities": ["cost-analysis", "margin-analysis", "cost-drivers",
                      "budget-variance", "program-profitability"],
     "identity": ("You are Cost Analysis Agent for an automotive enterprise. You "
                  "analyze cost structure and margins per vehicle model and "
                  "program, and investigate cost drivers and budget variances."),
     "boundary": ("You are analysis-only: you NEVER change prices, budgets or "
                  "financial records. Cost conclusions must trace to bound "
                  "financial/operational data; assumptions are explicit."),
     "reasoning": ("Break costs into material, labor, overhead and logistics "
                   "components where the data allows; compare across models and "
                   "periods; when a driver sits in another department, say which "
                   "specialist should investigate (and offer peer delegation)."),
     "tools": ("Use governed NL2SQL tools (list_data_sources, describe_schema, "
               "execute_query / ask_data_agent) for financial and operational "
               "data, ontology tools for model/program relationships, and "
               "delegate_task for production, procurement or sales peers."),
     "output": ("OUTPUT: lead with the margin verdict per model/program. Then "
                "the cost breakdown, variance drivers and evidence. Close with "
                "assumptions and data caveats.")},
    {"name": "Logistics & Distribution Agent", "project": "Logistics & Distribution",
     "description": ("Analyzes inbound and outbound logistics: transport lanes and "
                     "delivery performance, freight cost signals, distribution flow "
                     "for finished vehicles and parts, and delay-risk hotspots with "
                     "evidence-backed recommendations."),
     "capabilities": ["logistics-analysis", "delivery-performance", "freight-cost",
                      "distribution-flow", "delay-risk"],
     "identity": ("You are Logistics & Distribution Agent for an automotive "
                  "enterprise. You analyze transport lanes, delivery performance "
                  "and distribution flow for finished vehicles and parts."),
     "boundary": ("You are analysis-only: you NEVER book transport or change "
                  "shipments. Delivery conclusions must cite bound data; missing "
                  "lanes or periods are stated."),
     "reasoning": ("Rank lanes and routes by on-time performance and cost "
                   "signals, connect delays to root causes (supplier, port, "
                   "capacity), and propose actions a logistics planner could "
                   "approve."),
     "tools": ("Use governed NL2SQL tools (list_data_sources, describe_schema, "
               "execute_query / ask_data_agent) for logistics data, ontology "
               "tools for lane/route relationships, and delegate_task for "
               "supply-chain or production peers."),
     "output": ("OUTPUT: lead with the delivery performance verdict and top "
                "delay risks. For each: evidence, affected flows, and a "
                "recommended action. Close with assumptions and caveats.")},
]

_CUSTOMER_SUPPORT_CONFIG = {
    "description": ("Handles vehicle-owner inquiries and support tickets for "
                    "aftersales: intake, routing and status answers, grounded in "
                    "bound service data where available."),
    "capabilities": ["customer_support", "ticket_routing", "aftersales-inquiry"],
    "identity": ("You are Customer Support Agent for an automotive enterprise's "
                 "aftersales team. You handle vehicle-owner inquiries and support "
                 "tickets: understand the request, check bound service data where "
                 "authorized, and answer or route."),
    "boundary": ("You NEVER promise repairs, refunds or compensation — those "
                 "decisions belong to humans (escalate_to_human). Only quote "
                 "facts from bound data; if the answer is not in bound sources, "
                 "say so and escalate."),
    "reasoning": ("Classify the inquiry (service, warranty, delivery, general), "
                  "check bound data for case/vehicle context, and either answer "
                  "with evidence or route with a clear summary of what is known."),
    "tools": ("Use governed NL2SQL tools (list_data_sources, describe_schema, "
              "execute_query / ask_data_agent) for service data, and "
              "escalate_to_human for anything requiring a human decision."),
    "output": ("OUTPUT: answer the owner's question directly and courteously; "
               "for routed cases provide a summary the human agent can act on. "
               "Never invent case statuses or dates."),
}


def _resolve_project(db, Project, spec):
    p = None
    for name in filter(None, [spec["name"], spec.get("legacy")]):
        p = db.query(Project).filter(
            Project.name == name, Project.is_deleted == False).first()  # noqa: E712
        if p is not None:
            break
    return p


def main(dry_run: bool) -> int:
    from app.database import SessionLocal
    from app.models.project import Project
    from app.models.agent_app import AgentApp
    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    created_projects = created_agents = rebound = renamed = 0
    try:
        template = (
            db.query(Project)
            .filter(Project.name == "Manufacturing & Production",
                    Project.is_deleted == False).first()  # noqa: E712
        )
        org_id = template.org_id if template else "default-org"
        created_by = template.created_by_id if template else None

        # ── 1. Departments (upsert, reuse-first) ────────────────────────────
        projects_by_name = {}
        for spec in DEPARTMENTS:
            p = _resolve_project(db, Project, spec)
            if p is None:
                p = Project(
                    name=spec["name"], description=spec["description"],
                    status="active", is_system=False, resource_type="company",
                    department=spec["slug"], org_id=org_id,
                    created_by_id=created_by,
                )
                db.add(p)
                db.flush()
                created_projects += 1
                print(f"[project] CREATED  {spec['name']} (dept={spec['slug']})")
            else:
                if p.name != spec["name"]:
                    renamed += 1
                    print(f"[project] RENAMED {p.name} -> {spec['name']}")
                    # Keep the legacy KB name-string fallback consistent.
                    n = db.query(KnowledgeBase).filter(
                        KnowledgeBase.project == p.name,
                        KnowledgeBase.is_deleted == False).update(  # noqa: E712
                        {KnowledgeBase.project: spec["name"]})
                    if n:
                        print(f"[kb]      legacy project string updated on {n} KB(s)")
                    p.name = spec["name"]
                p.department = spec["slug"]
                p.description = spec["description"]
                p.status = "active"
                if p.resource_type != "company":
                    p.resource_type = "company"
                print(f"[project] REUSED   {p.name} (dept={spec['slug']})")
            projects_by_name[spec["name"]] = p

        # ── 2. Rebind existing agents (preserve IDs/prompts/KBs) ───────────
        for spec in REBIND:
            a = db.query(AgentApp).filter(
                AgentApp.name == spec["agent"],
                AgentApp.is_deleted == False).first()  # noqa: E712
            if a is None:
                print(f"[agent]   MISSING  {spec['agent']} (expected to exist — skipped)")
                continue
            proj = projects_by_name[spec["project"]]
            changed = []
            if a.project_id != proj.id:
                a.project_id = proj.id
                changed.append(f"project_id={proj.id[:8]}")
            if spec.get("publish") and a.status != "published":
                a.status = "published"
                changed.append("status=published")
            if spec.get("description"):
                a.description = spec["description"]
                changed.append("description enriched")
            if spec.get("capabilities_extra"):
                caps = list(a.capabilities or [])
                for c in spec["capabilities_extra"]:
                    if c not in caps:
                        caps.append(c)
                a.capabilities = caps
                changed.append("capabilities enriched")
            if spec.get("configure") == "customer_support":
                cfg = _CUSTOMER_SUPPORT_CONFIG
                a.description = cfg["description"]
                a.capabilities = cfg["capabilities"]
                a.prompt_identity = cfg["identity"]
                a.prompt_boundary = cfg["boundary"]
                a.prompt_reasoning = cfg["reasoning"]
                a.prompt_tools = cfg["tools"]
                a.prompt_output = cfg["output"]
                a.tool_config = {"enabled_tools": [
                    t for t in _BASE_TOOLS
                    if t not in ("delegate_task", "list_available_agents")]}
                a.policy_profile = {"risk_tier": "low", "requires_confirmation": False}
                changed.append("configured (prompts/tools/policy)")
            if changed:
                rebound += 1
                print(f"[agent]   REBOUND  {a.name}: {', '.join(changed)}")
            else:
                print(f"[agent]   OK       {a.name} (already bound)")

        # ── 3. Create genuinely missing specialists ────────────────────────
        for spec in NEW_AGENTS:
            existing = db.query(AgentApp).filter(
                AgentApp.name == spec["name"],
                AgentApp.is_deleted == False).first()  # noqa: E712
            proj = projects_by_name[spec["project"]]
            if existing is not None:
                if existing.project_id != proj.id:
                    existing.project_id = proj.id
                    print(f"[agent]   EXISTS   {spec['name']} rebound to {spec['project']}")
                else:
                    print(f"[agent]   EXISTS   {spec['name']} (already configured)")
                continue
            a = AgentApp(
                name=spec["name"], description=spec["description"],
                project="global", project_id=proj.id,
                capabilities=spec["capabilities"],
                tool_config={"enabled_tools": _BASE_TOOLS},
                prompt_identity=spec["identity"],
                prompt_boundary=_HONESTY + "\n\n" + spec["boundary"],
                prompt_reasoning=spec["reasoning"],
                prompt_tools=spec["tools"],
                prompt_output=spec["output"],
                policy_profile={"risk_tier": "low", "requires_confirmation": False},
                status="published", version=1, resource_type="company",
                is_system=False, memory_scope="user_only",
                max_call_count=80, max_iterations=30,
                agent_type="sequential", topology="standalone",
                knowledge_bases=[],
                org_id=org_id, created_by_id=created_by,
            )
            db.add(a)
            created_agents += 1
            print(f"[agent]   CREATED  {spec['name']} ({spec['project']}, published)")

        if dry_run:
            db.rollback()
            print("\n[DRY-RUN] rolled back — no changes written.")
        else:
            db.commit()
            print(f"\n[DONE] projects created={created_projects}, renamed={renamed}; "
                  f"agents rebound={rebound}, created={created_agents}; "
                  f"existing records preserved.")
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[ABORT] rolled back: {type(exc).__name__}: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
