"""feishu_drive tool — file management on Feishu Drive (Lark).

Light skeleton that mirrors the feishu_doc tool's pattern. Real
implementation needs lark_oapi + tenant access token; we expose the
schema so agents can use the tool name and the user can provide config
via update_env_config.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _feishu_drive(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["FEISHU_APP_ID", "FEISHU_APP_SECRET"])
    if missing:
        return missing_config_response("feishu_drive", missing_env=missing)
    action = (args.get("action") or "list").lower()
    return {
        "success": True,
        "action": action,
        "note": "feishu_drive skeleton registered; provide FEISHU_APP_ID + FEISHU_APP_SECRET to enable full lark_oapi integration.",
    }


FEISHU_DRIVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "feishu_drive",
        "description": "File management on Feishu Drive (Lark). Skeleton — see note.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "upload", "download", "delete"]},
                "folder_token": {"type": "string"},
                "file_name": {"type": "string"},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="feishu_drive",
    schema=FEISHU_DRIVE_SCHEMA,
    handler=_feishu_drive,
    category="communication",
    toolset="communication",
    description="Feishu Drive file management.",
    emoji="📁",
    requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
)
