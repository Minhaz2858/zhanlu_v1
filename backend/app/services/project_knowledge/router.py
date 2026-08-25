"""Admin router for the ProjectKnowledgeCache facade.

Endpoints (all admin-only):
  GET  /api/project-knowledge/{project_id}/stats
  POST /api/project-knowledge/{project_id}/reindex        body: {"kb_id": "..."}
  POST /api/project-knowledge/{project_id}/invalidate     body: {"scope": "all|links|metrics"}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.deps import get_current_user_required
from app.database import SessionLocal

from .cache import ProjectKnowledgeCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/project-knowledge", tags=["project-knowledge"])


def _db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ReindexBody(BaseModel):
    kb_id: str = Field(..., description="KnowledgeBase id to ingest")


class InvalidateBody(BaseModel):
    scope: str = Field("all", description="all | links | metrics")


def _require_flag():
    if not getattr(settings, "ECISCO_BI_KG_PROJECT_CACHE_ENABLED", False):
        raise HTTPException(
            status_code=503,
            detail="ECISCO_BI_KG_PROJECT_CACHE_ENABLED is False",
        )


@router.get("/{project_id}/stats")
def get_stats(
    project_id: str,
    user: Any = Depends(get_current_user_required),
    db=Depends(_db),
):
    _require_flag()
    cache = ProjectKnowledgeCache(project_id)
    return cache.stats(db).to_dict()


@router.post("/{project_id}/reindex")
async def reindex(
    project_id: str,
    body: ReindexBody,
    user: Any = Depends(get_current_user_required),
    db=Depends(_db),
):
    _require_flag()
    cache = ProjectKnowledgeCache(project_id)
    bg = getattr(settings, "PROJECT_KNOWLEDGE_INGEST_BG_WORKER", True)
    if bg:
        async def _bg():
            from .ingestion import ingest_for_project
            try:
                await ingest_for_project(project_id, body.kb_id, db)
            except Exception as e:
                logger.warning("background reindex failed: %s", e)

        asyncio.create_task(_bg())
        return {"status": "queued", "project_id": project_id, "kb_id": body.kb_id}
    status = cache.ingest(db, body.kb_id)
    return status.to_dict()


@router.post("/{project_id}/invalidate")
def invalidate(
    project_id: str,
    body: InvalidateBody,
    user: Any = Depends(get_current_user_required),
    db=Depends(_db),
):
    _require_flag()
    if body.scope not in ("all", "links", "metrics"):
        raise HTTPException(status_code=400, detail="scope must be all|links|metrics")
    cache = ProjectKnowledgeCache(project_id)
    deleted = cache.invalidate(db, scope=body.scope)  # type: ignore[arg-type]
    return {"deleted": deleted, "scope": body.scope, "project_id": project_id}
