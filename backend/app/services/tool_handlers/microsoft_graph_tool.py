"""microsoft_graph tool — Microsoft Graph API (Outlook, OneDrive, Teams).

Skeleton using MSAL for auth. Real implementation lazy-installs msal
on first use. For simple email/calendar ops, an app-only client_credential
flow can be configured.

Env vars: MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID. Optional:
MS_USER_ID (for app-only mail send).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _microsoft_graph(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"])
    if missing:
        return missing_config_response("microsoft_graph", missing_env=missing)
    action = (args.get("action") or "list_mail").lower()
    return {
        "success": True,
        "action": action,
        "note": "microsoft_graph skeleton registered. Real Graph API calls require the 'microsoft_graph' lazy-dep and proper MSAL config.",
    }


MICROSOFT_GRAPH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "microsoft_graph",
        "description": (
            "Microsoft Graph API (Outlook mail, calendar, OneDrive, "
            "Teams). Skeleton — requires MS_CLIENT_ID, MS_CLIENT_SECRET, "
            "MS_TENANT_ID."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list_mail", "send_mail", "list_calendar", "create_event"]},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="microsoft_graph",
    schema=MICROSOFT_GRAPH_SCHEMA,
    handler=_microsoft_graph,
    category="communication",
    toolset="communication",
    description="Microsoft Graph API (mail, calendar, files).",
    emoji="📧",
    requires_env=["MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"],
)
