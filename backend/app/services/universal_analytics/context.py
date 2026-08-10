"""Shared context helpers for universal analytics tools.

All tool handlers use these helpers to:
- Resolve which KBs are bound to the calling agent
- Check whether the UNIVERSAL_ANALYTICS_ENABLED flag is ON
- Return a structured "feature disabled" response when the flag is OFF
"""

from __future__ import annotations

import os
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase


def check_enabled() -> bool:
    """Return True if the UNIVERSAL_ANALYTICS_ENABLED flag is set."""
    return os.environ.get("UNIVERSAL_ANALYTICS_ENABLED", "true").lower() in (
        "true", "1", "yes",
    )


def missing_config_response(flag_name: str = "UNIVERSAL_ANALYTICS_ENABLED") -> dict:
    """Return a structured response when the feature is disabled."""
    return {
        "success": False,
        "error": (
            f"Universal analytics is disabled. "
            f"Set {flag_name}=true in the server environment to enable it."
        ),
    }


def get_bound_kbs(
    context: dict | None, db: Session
) -> list[KnowledgeBase]:
    """Return the database-type KnowledgeBases bound to this agent.

    Only returns KBs with source_kind == "db" (no files, no APIs).
    Returns an empty list when context is None or has no bound_kb_ids.
    """
    if not context:
        return []
    bound_ids = context.get("bound_kb_ids") or []
    if not bound_ids:
        return []
    kbs = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.id.in_(bound_ids),
            KnowledgeBase.is_deleted == False,
            KnowledgeBase.source_kind == "db",
        )
        .all()
    )
    return kbs


def get_first_db_kb(
    context: dict | None, db: Session, kb_id: Optional[str] = None
) -> KnowledgeBase | None:
    """Return the first database KB, or a specific one if kb_id given."""
    if kb_id:
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
            KnowledgeBase.source_kind == "db",
        ).first()
        return kb
    kbs = get_bound_kbs(context, db)
    return kbs[0] if kbs else None
