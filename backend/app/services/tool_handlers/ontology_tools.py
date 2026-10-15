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
