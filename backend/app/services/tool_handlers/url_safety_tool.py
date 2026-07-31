"""url_safety tool — re-export of the existing SSRF guard as a callable tool.

The underlying check lives in ``app.services.tool_security.is_safe_url`` and
is already used by web_search / web_extract / vision tools. This module
exposes the same check as an LLM-callable tool so agents can validate
arbitrary URLs before fetching them (e.g. in a custom workflow).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry
from app.services.tool_security import is_safe_url

logger = logging.getLogger(__name__)


async def _url_safety(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    url = (args.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "url is required"}
    safe = is_safe_url(url)
    return {
        "success": True,
        "url": url,
        "safe": safe,
        "verdict": "safe" if safe else "blocked",
        "note": (
            "Returns False for private/loopback/link-local IPs, cloud metadata "
            "endpoints (169.254.169.254), and unsupported schemes."
        ),
    }


URL_SAFETY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "url_safety",
        "description": (
            "Check whether a URL is safe to fetch (SSRF guard). Returns "
            "{safe: bool, verdict: 'safe'|'blocked'}. Always blocks cloud "
            "metadata endpoints and private/loopback IPs. Use before "
            "fetching arbitrary user-supplied URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to validate (http or https).",
                }
            },
            "required": ["url"],
        },
    },
}


registry.register(
    name="url_safety",
    schema=URL_SAFETY_SCHEMA,
    handler=_url_safety,
    category="web",
    toolset="web",
    description="SSRF guard: check if a URL is safe to fetch.",
    emoji="🛡️",
)
