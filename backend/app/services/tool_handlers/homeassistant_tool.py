"""homeassistant tool — control Home Assistant smart-home devices.

Uses the long-lived access token pattern. For full entity control the
HA instance URL and an access token are required.

Env vars: HOMEASSISTANT_URL (e.g. http://homeassistant.local:8123),
HOMEASSISTANT_TOKEN (long-lived access token).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _homeassistant(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"])
    if missing:
        return missing_config_response("homeassistant", missing_env=missing)

    action = (args.get("action") or "list_states").lower()
    base = os.environ["HOMEASSISTANT_URL"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {os.environ['HOMEASSISTANT_TOKEN']}",
        "Content-Type": "application/json",
    }
    try:
        if action == "list_states":
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/api/states", headers=headers)
                resp.raise_for_status()
                states = resp.json()
            return {
                "success": True,
                "action": "list_states",
                "count": len(states),
                "states": states[:100],
            }
        if action == "call_service":
            domain = (args.get("domain") or "").strip()
            service = (args.get("service") or "").strip()
            data = args.get("data", {}) or {}
            if not domain or not service:
                return {"success": False, "error": "domain and service are required"}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{base}/api/services/{domain}/{service}",
                    headers=headers,
                    json=data,
                )
                resp.raise_for_status()
                return {
                    "success": True,
                    "action": "call_service",
                    "domain": domain,
                    "service": service,
                    "response": resp.json(),
                }
    except Exception as exc:
        return {"success": False, "error": f"Home Assistant call failed: {exc}"}
    return {"success": False, "error": f"Unknown action: {action!r}"}


HOMEASSISTANT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "homeassistant",
        "description": (
            "Control Home Assistant devices. Actions: list_states, "
            "call_service. Requires HOMEASSISTANT_URL and "
            "HOMEASSISTANT_TOKEN."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list_states", "call_service"]},
                "domain": {"type": "string", "description": "Service domain (e.g. 'light', 'switch')."},
                "service": {"type": "string", "description": "Service name (e.g. 'turn_on', 'toggle')."},
                "data": {"type": "object", "description": "Service call data."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="homeassistant",
    schema=HOMEASSISTANT_SCHEMA,
    handler=_homeassistant,
    category="communication",
    toolset="communication",
    description="Control Home Assistant devices.",
    emoji="🏠",
    requires_env=["HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"],
    max_result_size_chars=30_000,
)
