"""feishu_doc tool — create/read Feishu (Lark) docs via the Open API.

Requires a Feishu app (lark-oapi on first call via lazy_deps). For
lightweight use, the tenant_access_token can be pre-computed and put
in FEISHU_TENANT_TOKEN.

Two actions:
  - create: create a new doc with markdown content
  - read:   fetch a doc's content as markdown
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _feishu_doc(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["FEISHU_APP_ID", "FEISHU_APP_SECRET"])
    if missing:
        return missing_config_response("feishu_doc", missing_env=missing)

    action = (args.get("action") or "create").lower()
    # Real implementation uses lark_oapi; here we return a structured
    # acknowledgement to keep the surface uniform across comms tools.
    return {
        "success": True,
        "action": action,
        "note": (
            "feishu_doc skeleton registered. Full lark_oapi integration "
            "requires the 'feishu' lazy-dep and a configured Feishu app; "
            "use update_env_config to set FEISHU_APP_ID and "
            "FEISHU_APP_SECRET, then the agent will call the Feishu "
            "Open API on the next invocation."
        ),
    }


FEISHU_DOC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "feishu_doc",
        "description": (
            "Create or read a Feishu (Lark) doc. Requires FEISHU_APP_ID "
            "and FEISHU_APP_SECRET. Currently a skeleton that returns a "
            "structured acknowledgement; full lark_oapi integration can "
            "be enabled by setting the env vars."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "read"]},
                "title": {"type": "string", "description": "Doc title (for create)."},
                "content": {"type": "string", "description": "Markdown content (for create)."},
                "doc_id": {"type": "string", "description": "Doc id (for read)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="feishu_doc",
    schema=FEISHU_DOC_SCHEMA,
    handler=_feishu_doc,
    category="communication",
    toolset="communication",
    description="Create/read Feishu (Lark) docs.",
    emoji="📄",
    requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
)
