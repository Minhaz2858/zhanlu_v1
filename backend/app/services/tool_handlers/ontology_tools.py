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
