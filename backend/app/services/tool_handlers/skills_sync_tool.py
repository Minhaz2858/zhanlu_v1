"""skills_sync tool — sync marketplace skills into the DB.

Wraps the existing ``app.services.skill_sync.sync_marketplace_to_db`` and
``reload_skills_registry`` functions. Use this after a fresh deploy or
when marketplace skills are not appearing in the catalog.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _skills_sync(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "sync_marketplace").lower()

    if action == "sync_marketplace":
        if db is None:
            return {"success": False, "error": "DB session required for marketplace sync"}
        try:
            from app.services.skill_sync import sync_marketplace_to_db
            count = sync_marketplace_to_db(db)
            return {
                "success": True,
                "action": "sync_marketplace",
                "synced": count,
            }
        except Exception as exc:
            return {"success": False, "error": f"Marketplace sync failed: {exc}"}

    if action == "reload_registry":
        try:
            from app.services.skill_sync import reload_skills_registry
            reload_skills_registry()
            return {"success": True, "action": "reload_registry"}
        except Exception as exc:
            return {"success": False, "error": f"Reload failed: {exc}"}

    return {"success": False, "error": f"Unknown action: {action!r}"}


SKILLS_SYNC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_sync",
        "description": (
            "Sync marketplace skills into the DB and/or reload the "
            "in-memory skills registry. Use after a fresh deploy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["sync_marketplace", "reload_registry"]},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="skills_sync",
    schema=SKILLS_SYNC_SCHEMA,
    handler=_skills_sync,
    category="skills",
    toolset="skills",
    description="Sync marketplace skills / reload registry.",
    emoji="🔄",
)
