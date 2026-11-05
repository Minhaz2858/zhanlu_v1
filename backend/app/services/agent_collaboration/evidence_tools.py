"""Evidence exchange tools for sub-agents (P2 Phase 2, Part 3).

These are the ONLY way a sub-agent contributes to or consumes shared evidence.
They are deliberately minimal: register a finding by reference, or look up a
reference another agent already produced. They never copy raw result sets between
agents — ``request_evidence`` returns *references* (provenance + compact summary),
not data.

Imported (and thus registered) from delegate_tool so they are available to the
child toolset without a new import surface.
"""

from __future__ import annotations

import logging

from app.services.tool_registry import registry
from app.services.agent_collaboration.shared_evidence import (
    VALIDATION_PENDING,
    VALIDATION_UNVALIDATED,
    VALIDATION_VALIDATED,
    VALIDATION_CONTRADICTED,
    provide_evidence,
    request_evidence_references,
)

logger = logging.getLogger(__name__)

_PROVIDE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "provide_evidence",
        "description": (
            "Register a finding you produced as SHARED EVIDENCE so a peer agent can "
            "reference it without you re-sending the data. Provide provenance "
            "(source/datasource/query) and a COMPACT findings summary — never paste "
            "full result rows. Returns an evidence_id to hand to peers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "string",
                    "description": "Compact summary of the numeric/structured finding (keep under ~400 chars).",
                },
                "source_id": {"type": "string", "description": "Datasource / KB id the finding came from."},
                "datasource_id": {"type": "string", "description": "Explicit datasource identifier."},
                "query_provenance": {
                    "type": "object",
                    "description": "Optional {sql, execution_id, nl} provenance for the query that produced this.",
                    "properties": {
                        "sql": {"type": "string"},
                        "execution_id": {"type": "string"},
                        "nl": {"type": "string"},
                    },
                },
                "metric_definition_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of metric definitions this finding instantiates.",
                },
                "freshness": {
                    "type": "object",
                    "description": "Optional {as_of, state} freshness metadata.",
                    "properties": {
                        "as_of": {"type": "string"},
                        "state": {"type": "string"},
                    },
                },
                "assumptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit assumptions behind the finding.",
                },
                "limitations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Known limitations / caveats.",
                },
                "validation_status": {
                    "type": "string",
                    "enum": [
                        VALIDATION_VALIDATED,
                        VALIDATION_UNVALIDATED,
                        VALIDATION_CONTRADICTED,
                        VALIDATION_PENDING,
                    ],
                    "description": "How checked this finding is (default unvalidated).",
                },
                "references": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "evidence_ids this finding builds on.",
                },
            },
            "required": ["findings"],
        },
    },
}

_REQUEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_evidence",
        "description": (
            "Ask peers for SHARED EVIDENCE on a topic. Returns compact REFERENCES "
            "(producer, source, validation status, findings summary) — NOT raw data. "
            "Use this instead of re-querying when another agent may already have the "
            "answer. Returns an empty list if no peer has produced matching evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Topic/keyword to search peer evidence for (e.g. 'supplier lead time', 'Q3 inventory').",
                },
            },
            "required": ["topic"],
        },
    },
}


def _conv_id_from(context):  # type: ignore[no-untyped-def]
    if not context:
        return None
    return context.get("conversation_id") or context.get("execution_id")


async def provide_evidence_tool(args: dict, db, user_id, context=None) -> dict:
    conv_id = _conv_id_from(context)
    if not conv_id:
        return {"success": False, "error": "evidence requires a conversation/execution id"}
    producer = (context or {}).get("agent_name") or (context or {}).get("requesting_agent_id") or "unknown-agent"
    try:
        eid = provide_evidence(
            conv_id, producer, source_id=args.get("source_id"),
            datasource_id=args.get("datasource_id"),
            query_provenance=args.get("query_provenance"),
            metric_definition_ids=args.get("metric_definition_ids"),
            findings=args.get("findings"),
            freshness=args.get("freshness"),
            assumptions=args.get("assumptions"),
            limitations=args.get("limitations"),
            validation_status=args.get("validation_status", VALIDATION_UNVALIDATED),
            references=args.get("references"),
        )
        return {"success": True, "evidence_id": eid, "validation_status": args.get("validation_status", VALIDATION_UNVALIDATED)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}


async def request_evidence_tool(args: dict, db, user_id, context=None) -> dict:
    conv_id = _conv_id_from(context)
    if not conv_id:
        return {"success": True, "references": [], "note": "no conversation context"}
    refs = request_evidence_references(conv_id, args.get("topic", ""))
    return {"success": True, "references": refs, "count": len(refs)}


def register_evidence_tools() -> None:
    registry.register(
        name="provide_evidence",
        schema=_PROVIDE_SCHEMA,
        handler=provide_evidence_tool,
        category="collaboration",
        enabled_by_default=False,  # only sub-agents get these (added to child toolset)
        description="Register a finding as shared evidence (by reference).",
    )
    registry.register(
        name="request_evidence",
        schema=_REQUEST_SCHEMA,
        handler=request_evidence_tool,
        category="collaboration",
        enabled_by_default=False,
        description="Request shared-evidence references from peer agents.",
    )
