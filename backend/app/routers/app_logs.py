"""Telemetry sink — accepts page-view pings from the Base44 vite plugin.

The Base44 vite plugin (`@base44/vite-plugin/html-injections-plugin.ts:27`)
fires a fire-and-forget POST to ``/api/app-logs/{appId}/log-user-in-app/{pageName}``
on every client-side navigation. The original Base44 backend had a pageview
tracker; Zhanlu never implemented a counterpart because it has its own
analytics stack. Without this stub the request 404s on every page load and
clutters the backend log with no functional impact.

Behaviour: accepts the call, logs nothing by default, returns a 200 no-op
JSON. Auth is intentionally NOT required — it fires before the auth check
on first paint and the SDK doesn't send a token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)


router = APIRouter(tags=["telemetry"], include_in_schema=False)


@router.post("/app-logs/{app_id}/log-user-in-app/{page_name}")
async def log_user_in_app(app_id: str, page_name: str) -> dict:
    """Best-effort pageview sink — see module docstring."""
    # The Base44 SDK page name uses bare segments (e.g. "home", "settings").
    # We log at DEBUG so it doesn't pollute the normal log level but is still
    # available when debugging clickthrough patterns.
    logger.debug("pageview sink: app=%s page=%s", app_id, page_name)
    return {"logged": False, "reason": "telemetry_stub"}
