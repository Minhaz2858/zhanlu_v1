"""approval tool — explicit governance approval request.

Zhanlu already has a permission/approval flow via
``app.services.permissions`` and ``ApprovalService``. This tool exposes
the same capability as a tool the agent can call directly when it
wants to request user sign-off on a specific action.

The actual UI integration is via the per-conversation event queue
(``app.services.tool_output.ui_events``); the chat SSE endpoint drains
it on the next tick.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _approval(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action_type = (args.get("action_type") or "").strip()
    description = (args.get("description") or "").strip()
    risk = (args.get("risk_tier") or "medium").lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    if not action_type:
        return {"success": False, "error": "action_type is required"}
    if not description:
        return {"success": False, "error": "description is required"}

    request_id = uuid.uuid4().hex[:8]
    payload = {
        "approval_id": request_id,
        "action_type": action_type,
        "description": description,
        "risk_tier": risk,
        "issued_at": time.time(),
        "status": "pending",
    }
    # Try to persist via the existing ApprovalService
    persisted = False
    if db is not None:
        try:
            from app.services.governance.approval_service import ApprovalService
            svc = ApprovalService(db)
            record = svc.create_request(
                action_type=action_type,
                action_description=description,
                risk_tier=risk,
                context_json=args.get("context", {}),
                ttl_hours=int(args.get("ttl_hours", 1)),
            )
            payload["approval_id"] = record.id
            persisted = True
        except Exception as exc:
            logger.debug("ApprovalService unavailable, falling back to event queue: %s", exc)
    payload["persisted"] = persisted

    # Push to the UI event queue
    conversation_id = (context or {}).get("conversation_id") if context else None
    if conversation_id:
        try:
            from app.services.tool_output import push_event
            push_event(conversation_id, {"type": "approval_request", **payload})
        except Exception as exc:
            logger.debug("Could not push approval event: %s", exc)

    return {
        "success": True,
        "instruction": (
            "Surface this approval request to the user (queued for the "
            "chat UI). Do NOT perform the action until the user approves."
        ),
        **payload,
    }


APPROVAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "approval",
        "description": (
            "Request explicit user approval before performing an action. "
            "Persists the request via the existing ApprovalService when "
            "available; falls back to the per-conversation event queue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "description": "Short action type (e.g. 'send_email', 'delete_record')."},
                "description": {"type": "string", "description": "Plain-language description of the action."},
                "risk_tier": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
                "context": {"type": "object", "description": "Optional structured context to attach to the request."},
                "ttl_hours": {"type": "integer", "description": "How long the request stays valid (default 1h).", "default": 1},
            },
            "required": ["action_type", "description"],
        },
    },
}

registry.register(
    name="approval",
    schema=APPROVAL_SCHEMA,
    handler=_approval,
    category="governance",
    toolset="governance",
    description="Request explicit user approval before performing an action.",
    emoji="✋",
)
