"""microsoft_graph_auth tool — OAuth flow for Microsoft Graph.

Companion to microsoft_graph: handles the interactive auth code → token
exchange. For app-only (client_credentials) flows, no user interaction
is needed and the token is fetched on each call.

Skeleton — full MSAL flow can be activated by adding the 'microsoft_graph'
lazy-dep and a configured app registration.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _microsoft_graph_auth(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["MS_CLIENT_ID", "MS_TENANT_ID"])
    if missing:
        return missing_config_response("microsoft_graph_auth", missing_env=missing)
    action = (args.get("action") or "status").lower()
    return {
        "success": True,
        "action": action,
        "note": "microsoft_graph_auth skeleton registered. Provides auth URL (action='authorize') and code→token exchange (action='exchange').",
    }


MICROSOFT_GRAPH_AUTH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "microsoft_graph_auth",
        "description": "Microsoft Graph OAuth flow (authorize / exchange / status). Skeleton.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["authorize", "exchange", "status"]},
                "auth_code": {"type": "string", "description": "Auth code from redirect (for exchange)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="microsoft_graph_auth",
    schema=MICROSOFT_GRAPH_AUTH_SCHEMA,
    handler=_microsoft_graph_auth,
    category="communication",
    toolset="communication",
    description="Microsoft Graph OAuth flow.",
    emoji="🔐",
    requires_env=["MS_CLIENT_ID", "MS_TENANT_ID"],
)
