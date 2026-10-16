"""Ontology agent tools (P1-3 + P2-1).

Registered in the global ToolRegistry → agents call them in the loop AND
they are automatically exposed as MCP tools (services/mcp_server wraps
the registry).

- notify_ontology_change: fan out a business-object change to the agents
  bound to LINKED objects' domains (ontology-link traversal).
- ontology_directory: the org's semantic-layer directory (domains →
  objects → actions) so any agent can discover business nouns/verbs.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


def _org_id(context: dict | None) -> str:
    return (context or {}).get("org_id") or "default-org"


async def _notify_ontology_change(args: dict, db: Session, user_id: str | None,
                                  context: dict | None = None) -> dict:
    object_name = str(args.get("object_name") or "").strip()
    change_summary = str(args.get("change_summary") or "").strip()
    if not object_name or not change_summary:
        return {"success": False,
                "reason": "object_name and change_summary are required"}
    from app.services.ontology.fanout import notify_object_change

    return notify_object_change(
        db, _org_id(context), object_name, change_summary,
        changed_by=user_id, team_id=args.get("team_id"),
    )


NOTIFY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "notify_ontology_change",
        "description": (
            "Fan out a business-object change through the ontology: agents "
            "bound to LINKED objects' domains are notified (audit-logged). "
            "Use when something material changed (a signal hit a supplier, "
            "a contract moved, stock crossed a threshold) so OTHER domain "
            "agents can react within their guardrails."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "object_name": {"type": "string",
                                "description": "Ontology object that changed (e.g. Supplier)."},
                "change_summary": {"type": "string",
                                   "description": "WHAT changed, concretely."},
                "team_id": {"type": "string",
                            "description": "Optional swarm team id for mailbox delivery."},
            },
            "required": ["object_name", "change_summary"],
        },
    },
}


async def _ontology_directory(args: dict, db: Session, user_id: str | None,
                              context: dict | None = None) -> dict:
    from app.services.ontology.queries import (
        list_actions,
        list_domains,
        list_objects,
    )
    from app.services.ontology.serialize import (
        action_to_dict,
        domain_to_dict,
        object_to_dict,
    )

    org_id = _org_id(context)
    domains = list_domains(db, org_id)
    objects = list_objects(db, org_id)
    actions = list_actions(db, org_id)
    domain_filter = args.get("domain")
    if domain_filter:
        domains = [d for d in domains if d.code == domain_filter]
    return {
        "success": True,
        "domains": [
            {
                **domain_to_dict(d),
                "objects": [object_to_dict(o) for o in objects
                            if o.domain_id == d.id],
                "actions": [action_to_dict(a) for a in actions
                            if any(o.id == a.object_id and o.domain_id == d.id
                                   for o in objects)],
            }
            for d in domains
        ],
    }


DIRECTORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ontology_directory",
        "description": (
            "List the org's ontology directory: business domains, their "
            "objects (with live table bindings) and available actions. "
            "Read-only. Use it to ground yourself in the business "
            "vocabulary before answering domain questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string",
                           "description": "Optional domain code filter (e.g. procurement)."},
            },
        },
    },
}


registry.register(
    name="notify_ontology_change",
    schema=NOTIFY_SCHEMA,
    handler=_notify_ontology_change,
    category="data",
    toolset="data",
    description="Fan out a business-object change via ontology links.",
    emoji="📡",
    max_result_size_chars=20_000,
)

registry.register(
    name="ontology_directory",
    schema=DIRECTORY_SCHEMA,
    handler=_ontology_directory,
    category="data",
    toolset="data",
    description="Org semantic-layer directory (domains/objects/actions).",
    emoji="🗂️",
    max_result_size_chars=40_000,
)


# ── P3-6: escalation-with-context ────────────────────────────────────────

async def _escalate_to_human(args: dict, db: Session, user_id: str | None,
                             context: dict | None = None) -> dict:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return {"success": False,
                "reason": "summary is required — what does the human need to know?"}
    from app.services.ontology.escalation import create_escalation

    return create_escalation(
        db, _org_id(context), summary,
        context=args.get("context"),
        conversation_id=(context or {}).get("conversation_id")
        or args.get("conversation_id"),
        agent_name=(context or {}).get("agent_name")
        or args.get("agent_name"),
        object_name=args.get("object_name"),
        sentiment=args.get("sentiment"),
        created_by=user_id,
    )


ESCALATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "escalate_to_human",
        "description": (
            "Hand off to a human WITH full context. Call when: the user "
            "is frustrated or asks for a human; the issue cannot be "
            "resolved with your tools; or the matter is high-stakes and "
            "needs human judgment. Package WHAT the user needs, what you "
            "already tried, and any data you collected — never leave a "
            "dead end."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "What the human needs to know and do."},
                "context": {
                    "type": "object",
                    "description": "What you tried, collected data, relevant IDs.",
                },
                "object_name": {"type": "string",
                                "description": "Optional ontology object (e.g. Ticket) whose linked domains get notified."},
                "sentiment": {"type": "string",
                              "description": "frustrated | asks_human | stuck | negative."},
                "conversation_id": {"type": "string"},
                "agent_name": {"type": "string"},
            },
            "required": ["summary"],
        },
    },
}

registry.register(
    name="escalate_to_human",
    schema=ESCALATE_SCHEMA,
    handler=_escalate_to_human,
    category="data",
    toolset="data",
    description="Structured human handoff with full context transfer.",
    emoji="🙋",
    max_result_size_chars=20_000,
)


# ── P3-5: lead scoring (IBM sales/CS: qualification & prioritization) ────

async def _score_leads(args: dict, db: Session, user_id: str | None,
                       context: dict | None = None) -> dict:
    object_name = str(args.get("object_name") or "").strip()
    criteria = args.get("criteria")
    if not object_name or not criteria:
        return {"success": False,
                "reason": "object_name and criteria are required"}
    if isinstance(criteria, str):
        criteria = [c.strip() for c in criteria.split(";") if c.strip()]
    limit = min(int(args.get("limit") or 20), 50)

    from app.services.ontology.data import read_object_data
    from app.services.ontology.queries import get_object_by_name

    org_id = _org_id(context)
    obj = get_object_by_name(db, org_id, object_name)
    if not obj:
        return {"success": False,
                "reason": f"object {object_name!r} not found in the ontology"}
    if not (obj.backing_ref and obj.backing_kind != "manual"):
        return {"success": False,
                "reason": (f"object {object_name!r} is not bound to live data — "
                           "run discovery first (never score invented rows)")}

    read = read_object_data(db, org_id, obj, limit=limit)
    if not read.get("success"):
        return {"success": False, "reason": read.get("reason", "read failed")}
    rows = read.get("rows") or []
    if not rows:
        return {"success": True, "scores": [], "note": "no rows to score"}

    from app.services.llm_service import chat_completion_json_sync

    prompt = (
        "You are a B2B lead qualifier. Score each record 0-100 against "
        "the criteria. Use ONLY the fields present in each record — do "
        "not invent data. Be consistent and strict.\n\n"
        f"Criteria:\n- " + "\n- ".join(criteria) + "\n\n"
        "Records (JSON):\n" + json.dumps(rows, ensure_ascii=False, default=str)[:8000]
        + "\n\nAnswer JSON: {\"scores\": [{\"index\": <0-based row index>, "
          "\"score\": <0-100>, \"reason\": \"one sentence citing the fields\"}]}"
    )
    schema = {
        "type": "object",
        "properties": {"scores": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"},
                           "score": {"type": "number"},
                           "reason": {"type": "string"}},
            "required": ["index", "score"],
        }}},
        "required": ["scores"],
    }
    try:
        verdict = chat_completion_json_sync(prompt, schema=schema, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 — scoring is advisory
        return {"success": False, "reason": f"scoring LLM failed: {exc}"}
    raw = (verdict or {}).get("scores") or []
    by_index = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
            score = max(0.0, min(100.0, float(item.get("score"))))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(rows):
            by_index[idx] = {"score": score,
                             "reason": str(item.get("reason") or "")[:300]}

    scored = []
    for i, row in enumerate(rows):
        entry = dict(row)
        if i in by_index:
            entry["lead_score"] = by_index[i]["score"]
            entry["score_reason"] = by_index[i]["reason"]
        else:
            entry["lead_score"] = None  # honest: not scored, not zero
        scored.append(entry)
    scored.sort(key=lambda r: (r["lead_score"] is None,
                               -(r["lead_score"] or 0)))
    return {
        "success": True, "object": object_name, "criteria": criteria,
        "scores": scored,
        "note": "scores are LLM-judged against your criteria from LIVE rows",
    }


SCORE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "score_leads",
        "description": (
            "Score/prioritize records (leads, accounts, deals, tickets) "
            "from LIVE bound data against your stated criteria. Read-only; "
            "every score cites the fields it used. Use for lead "
            "qualification, pipeline prioritization, or triage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "object_name": {"type": "string",
                                "description": "Ontology object holding the records (e.g. Customer, Lead)."},
                "criteria": {
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "Qualification criteria (e.g. 'enterprise segment; annual revenue > 1M; onboarded < 1 year')."},
                "limit": {"type": "integer", "description": "Max rows to score (default 20, max 50)."},
            },
            "required": ["object_name", "criteria"],
        },
    },
}

registry.register(
    name="score_leads",
    schema=SCORE_SCHEMA,
    handler=_score_leads,
    category="data",
    toolset="data",
    description="Qualify/prioritize live records against criteria (0-100 + reasons).",
    emoji="🎯",
    max_result_size_chars=40_000,
)


# ── P3-14: segmentation over live data (read-only GROUP BY) ─────────────

async def _segment_records(args: dict, db: Session, user_id: str | None,
                           context: dict | None = None) -> dict:
    import re as _re

    object_name = str(args.get("object_name") or "").strip()
    segment_by = str(args.get("segment_by") or "").strip()
    if not object_name or not segment_by:
        return {"success": False,
                "reason": "object_name and segment_by are required"}

    from app.services.ontology.queries import get_object_by_name
    from app.services.db.query_service import QueryService

    org_id = _org_id(context)
    obj = get_object_by_name(db, org_id, object_name)
    if not obj:
        return {"success": False,
                "reason": f"object {object_name!r} not found in the ontology"}
    if not (obj.backing_ref and obj.backing_kind != "manual"):
        return {"success": False,
                "reason": (f"object {object_name!r} is not bound to live data — "
                           "run discovery first (never segment invented rows)")}
    # resolve the physical column for the segment property
    prop = next((p for p in (obj.properties_json or [])
                 if isinstance(p, dict) and p.get("name") == segment_by), None)
    col = prop.get("column") if prop else None
    if not col or not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", col):
        return {"success": False,
                "reason": (f"property {segment_by!r} has no verified column — "
                           "map it via the bind endpoint")}
    if not obj.backing_ref.startswith("kb:"):
        return {"success": False,
                "reason": "segmentation supports tenant-database objects (kb: refs)"}
    parts = obj.backing_ref.split(":", 2)  # kb:<kb_id>:<table>
    kb_id, table = parts[1], parts[2]
    if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
        return {"success": False, "reason": "backing table name is not a safe identifier"}

    sql = (f"SELECT {col} AS segment, COUNT(*) AS n FROM {table} "
           f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY n DESC LIMIT 15")
    try:
        result = QueryService(db).execute(kb_id, sql, max_rows=15, timeout_s=20)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"segment query failed: {exc}"}
    segments = [
        {"segment": r.get("segment"), "count": r.get("n")}
        for r in (result.get("rows") or [])
    ]
    total = sum(s["count"] or 0 for s in segments)
    return {
        "success": True, "object": object_name, "segmented_by": segment_by,
        "total_counted": total, "segments": segments,
        "note": ("live counts from the bound table — top "
                 f"{len(segments)} segments by size"),
    }


SEGMENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "segment_records",
        "description": (
            "Segment live records by any mapped property (read-only "
            "GROUP BY on the bound table). Use for audience segmentation, "
            "portfolio breakdowns, or campaign targeting inputs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "object_name": {"type": "string",
                                "description": "Ontology object (e.g. Customer)."},
                "segment_by": {"type": "string",
                               "description": "Property to segment by (must be mapped to a column)."},
            },
            "required": ["object_name", "segment_by"],
        },
    },
}

registry.register(
    name="segment_records",
    schema=SEGMENT_SCHEMA,
    handler=_segment_records,
    category="data",
    toolset="data",
    description="Segment live records by a mapped property (GROUP BY counts).",
    emoji="🧩",
    max_result_size_chars=20_000,
)


# ── P3-13: campaign planning (read-tier — plans, never sends) ───────────

async def _plan_campaign(args: dict, db: Session, user_id: str | None,
                         context: dict | None = None) -> dict:
    from app.services.llm_service import chat_completion_json_sync
    from app.services.ontology.queries import get_object_by_name
    from app.services.ontology.serialize import object_to_dict

    objective = str(args.get("objective") or "").strip()
    if not objective:
        return {"success": False, "reason": "objective is required"}
    org_id = _org_id(context)

    # ground the plan in real audience data when an object is given
    audience_block = ""
    object_name = args.get("object_name")
    if object_name:
        obj = get_object_by_name(db, org_id, str(object_name))
        if obj and obj.backing_ref and obj.backing_kind != "manual":
            d = object_to_dict(obj)
            audience_block = (
                f"\nAudience object (REAL, bound to live data): "
                f"{d.get('name')} — properties: "
                f"{[p.get('name') for p in (d.get('properties') or [])]}\n"
                "Base channel/step decisions on these properties; do not "
                "assume data you have not seen."
            )

    prompt = (
        "You are a senior marketing strategist. Produce a concrete, "
        "executable campaign plan for the objective. Be specific: real "
        "channel names, ordered steps with owners, measurable KPIs with "
        "target numbers, and per-segment message angles. Do not invent "
        "customer data." + audience_block + "\n\n"
        f"Objective: {objective}\n"
        f"Additional notes: {args.get('notes') or 'none'}\n\n"
        'Answer JSON: {"theme": str, "channels": [str], '
        '"steps": [{"order": int, "action": str, "owner": str}], '
        '"kpis": [{"name": str, "target": str}], '
        '"messages": [{"segment": str, "angle": str}]}'
    )
    schema = {
        "type": "object",
        "properties": {
            "theme": {"type": "string"},
            "channels": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": {"type": "object"}},
            "kpis": {"type": "array", "items": {"type": "object"}},
            "messages": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["theme", "channels", "steps", "kpis"],
    }
    try:
        plan = chat_completion_json_sync(prompt, schema=schema, temperature=0.3)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"planning LLM failed: {exc}"}
    if not plan or not plan.get("theme"):
        return {"success": False, "reason": "planner returned no plan"}
    return {
        "success": True, "objective": objective, "plan": plan,
        "note": ("plan only — outbound sends and ad spend remain "
                 "human-executed (risk policy)"),
    }


CAMPAIGN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "plan_campaign",
        "description": (
            "Produce a structured marketing campaign plan (theme, "
            "channels, ordered steps, KPIs, per-segment messages) for an "
            "objective. Read-tier: it PLANS — outbound execution stays "
            "with humans."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {"type": "string",
                              "description": "What the campaign must achieve."},
                "object_name": {"type": "string",
                                "description": "Optional ontology object to ground audience decisions."},
                "notes": {"type": "string", "description": "Constraints, budget, timing."},
            },
            "required": ["objective"],
        },
    },
}

registry.register(
    name="plan_campaign",
    schema=CAMPAIGN_SCHEMA,
    handler=_plan_campaign,
    category="data",
    toolset="data",
    description="Structured campaign plan (theme/channels/steps/KPIs) — plans, never sends.",
    emoji="📣",
    max_result_size_chars=30_000,
)
