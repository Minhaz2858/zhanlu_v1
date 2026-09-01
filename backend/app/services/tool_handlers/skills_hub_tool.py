"""skills_hub tool — multi-source skills aggregator.

Lists skills from all known sources (DB tools table, marketplace,
filesystem, user-installed). Use this when the agent needs a full
catalogue view across sources.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _skills_hub(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    sources: List[dict] = []

    # Source 1: DB tools table
    if db is not None:
        try:
            from app.models.tool import Tool
            tools = db.query(Tool).filter(Tool.is_deleted == False).all()  # noqa: E712
            sources.append({
                "source": "db_tools",
                "count": len(tools),
                "items": [
                    {
                        "name": t.name,
                        "category": getattr(t, "category", "") or "",
                        "description": (getattr(t, "description", "") or "")[:120],
                        "trigger": getattr(t, "trigger", "") or "",
                        "kind": getattr(t, "kind", "") or "",
                        "status": getattr(t, "status", "") or "",
                    }
                    for t in tools
                ],
            })
        except Exception as exc:
            sources.append({"source": "db_tools", "error": str(exc)})

    # Source 2: marketplace
    if db is not None:
        try:
            from app.models.market_agent import MarketAgent
            market = db.query(MarketAgent).filter(MarketAgent.is_deleted == False).all()  # noqa: E712
            sources.append({
                "source": "marketplace",
                "count": len(market),
                "items": [
                    {
                        "name": a.name,
                        "category": getattr(a, "category", "") or "",
                        "description": (getattr(a, "description", "") or "")[:120],
                    }
                    for a in market
                ],
            })
        except Exception as exc:
            sources.append({"source": "marketplace", "error": str(exc)})

    # Source 3: filesystem skills
    try:
        from app.services.skills_loader import unified_search
        fs_results = unified_search("", limit=200, db=db)
        sources.append({
            "source": "filesystem",
            "count": len(fs_results),
            "items": fs_results,
        })
    except Exception as exc:
        sources.append({"source": "filesystem", "error": str(exc)})

    return {"success": True, "sources": sources}


SKILLS_HUB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_hub",
        "description": (
            "Aggregate skills from all known sources: the DB tools table, "
            "the marketplace, and the filesystem. Use this to get a "
            "global view of available capabilities."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

registry.register(
    name="skills_hub",
    schema=SKILLS_HUB_SCHEMA,
    handler=_skills_hub,
    category="skills",
    toolset="skills",
    description="Multi-source skills aggregator.",
    emoji="🌐",
    max_result_size_chars=30_000,
)
