"""query_ontology tool — agents read the enterprise's business objects
(P0-2 of the Enterprise AI OS strategy, 2026-10-15).

The strategy's key design rule: agents never touch raw tables directly,
they operate through ontology objects and actions. This tool resolves a
business object ("Product", "Supplier", "Order" …) against the org's
ontology, reads its GROUNDED live data (identifier-validated, RLS-
respecting) and returns rows + provenance — the answer-contract the
orchestrator renders from.

Companion: execute_ontology_action fires a domain action under the risk
contract (read=auto, write=blocked → governance approval, P0-3).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings  # noqa: F401 — tool enable flag
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


def _org_and_agent(ctx: dict) -> tuple[str, str]:
    org_id = ctx.get("org_id") or "default-org"
    agent_name = ctx.get("agent_name") or "general_assistant"
    return org_id, agent_name


def _query_ontology(args: dict, db: Session, user_id: str | None,
                    context: dict | None = None) -> dict:
    from app.services.ontology import (
        get_object_by_name,
        list_domains,
        read_object_data,
    )
    from app.services.ontology.queries import ensure_domains

    ctx = context or {}
    org_id, _agent = _org_and_agent(ctx)
    obj_name = (args.get("object") or "").strip()
    if not obj_name:
        # No object named: return the object catalog so the LLM can choose.
        ensure_domains(db, org_id)
        domains = list_domains(db, org_id)
        return {
            "success": True,
            "catalog": [
                {"domain": d.code, "name": d.name} for d in domains
            ],
            "note": "Pass `object` with a business object name to read its data.",
        }

    obj = get_object_by_name(db, org_id, obj_name)
    if not obj:
        ensure_domains(db, org_id)
        obj = get_object_by_name(db, org_id, obj_name)
    if not obj:
        return {
            "success": False,
            "reason": f"object {obj_name!r} not in the ontology — "
                      "call with no `object` to see the catalog",
        }

    return read_object_data(
        db, org_id, obj,
        filters=args.get("filters") or {},
        limit=args.get("limit") or 50,
    )


def _execute_ontology_action(args: dict, db: Session, user_id: str | None,
                             context: dict | None = None) -> dict:
    from app.services.ontology import execute_action

    ctx = context or {}
    org_id, _agent = _org_and_agent(ctx)
    action_id = (args.get("action_id") or "").strip()
    if not action_id:
        return {"success": False,
                "reason": "action_id is required (from the ontology overview)"}
    return execute_action(
        db, org_id, user_id, action_id,
        params=args.get("params") or {}, context=ctx,
    )


QUERY_ONTOLOGY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_ontology",
        "description": (
            "Read a business object from the org's enterprise ontology "
            "(e.g. Product, Supplier, Customer, Order) with grounded live "
            "rows. Call without `object` to list the available business "
            "objects. Prefer this over raw SQL — it carries business "
            "semantics and provenance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "object": {"type": "string",
                           "description": "Business object name, e.g. 'Product'."},
                "filters": {"type": "object",
                            "description": "Optional property equality filters."},
                "limit": {"type": "integer", "description": "Max rows (default 50)."},
            },
        },
    },
}

EXECUTE_ACTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_ontology_action",
        "description": (
            "Execute an ontology action (a business verb bound to a tool). "
            "Read-type actions run immediately; write-type actions require "
            "human confirmation and will be blocked pending approval — "
            "report that honestly to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string",
                              "description": "The ontology action id."},
                "params": {"type": "object",
                           "description": "Action arguments."},
            },
            "required": ["action_id"],
        },
    },
}

if getattr(settings, "ONTOLOGY_TOOL_ENABLED", True):
    registry.register(
        name="query_ontology",
        schema=QUERY_ONTOLOGY_SCHEMA,
        handler=_query_ontology,
        category="data",
        toolset="data",
        description="Read business objects from the org's enterprise ontology.",
        emoji="🧬",
        max_result_size_chars=100_000,
    )
    registry.register(
        name="execute_ontology_action",
        schema=EXECUTE_ACTION_SCHEMA,
        handler=_execute_ontology_action,
        category="data",
        toolset="data",
        description="Execute a business action (risk-tiered: read auto, write gated).",
        emoji="⚡",
        max_result_size_chars=60_000,
    )
