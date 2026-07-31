"""Marketplace router — multi-source skill aggregator.

Replaces the old community-publish router. Endpoints:
  GET    /api/marketplace/sources               — list sources
  POST   /api/marketplace/sources               — add source
  DELETE /api/marketplace/sources/{id}          — remove/hide source
  POST   /api/marketplace/sources/{id}/sync     — trigger re-sync
  GET    /api/marketplace/sources/{id}/skills   — list skills for a source
  GET    /api/marketplace/skills/{id}           — external skill detail
  POST   /api/marketplace/skills/{id}/install   — install to My Skills (runtime)
  GET    /api/marketplace/my-skills             — list installed skills
  DELETE /api/marketplace/my-skills/{tool_id}   — remove from My Skills
  GET    /api/marketplace/curated               — list curated defaults
"""

import logging
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplace", tags=["marketplace"], dependencies=[Depends(get_current_user_required)])


class AddSourceRequest(BaseModel):
    url: str
    name: Optional[str] = None
    source_type: Optional[str] = None
    description: Optional[str] = None
    # Visual branding for the source card. Both optional — server fills in
    # sensible defaults if the caller omits them.
    brand_color: Optional[str] = None
    icon_emoji: Optional[str] = None


def _source_to_dict(src) -> dict:
    return {
        "id": src.id,
        "name": src.name,
        "url": src.url,
        "source_type": src.source_type,
        "description": src.description,
        "is_default": src.is_default,
        "is_hidden": src.is_hidden,
        "last_synced_at": src.last_synced_at.isoformat() if src.last_synced_at else None,
        "last_sync_status": src.last_sync_status,
        "last_sync_error": src.last_sync_error,
        "skill_count": src.skill_count,
        "brand_color": src.brand_color,
        "icon_emoji": src.icon_emoji,
    }


def _external_skill_to_dict(sk) -> dict:
    return {
        "id": sk.id,
        "source_id": sk.source_id,
        "name": sk.name,
        "display_name": sk.display_name,
        "description": sk.description,
        "summary": sk.summary,
        "category": sk.category,
        "version": sk.version,
        "author": sk.author,
        "tags": sk.tags or [],
        "source_url": sk.source_url,
        "github_url": sk.github_url,
        "install_count": sk.install_count,
    }


def _tool_to_dict(t) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "category": t.category,
        "version": t.version,
        "publisher": t.publisher,
        "github_url": t.github_url,
        "created_date": t.created_date.isoformat() if t.created_date else None,
    }


# ─── Sources ────────────────────────────────────────────────────────────

@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    from app.services.skill_source_service import (
        seed_curated_sources,
        get_curated_sources_needing_sync,
        sync_source,
    )
    from app.models.skill_source import SkillSource
    seed_curated_sources(db)
    # Auto-sync any curated source that has never been synced so the user
    # doesn't have to click Sync on every default card. We try to
    # schedule into the running event loop first (the real browser
    # request runs async, so the task persists and the response returns
    # immediately); if there's no running loop (tests, startup
    # scripts), fall back to a daemon thread so the sync still happens
    # — without this, the cards would stay at "0 skills" until the user
    # clicked Sync manually on every default card.
    for src in get_curated_sources_needing_sync(db):
        try:
            loop = asyncio.get_running_loop()
            # Pass None so the background task gets its own session —
            # the request-scoped session is closed by the time the task
            # actually runs.
            loop.create_task(sync_source(src.id))
        except RuntimeError:
            # No running event loop. Spin up a daemon thread to run
            # the sync. The thread opens its own session inside
            # sync_source(None) so the request session's lifetime is
            # irrelevant here.
            import threading
            def _run():
                asyncio.run(sync_source(src.id))
            t = threading.Thread(target=_run, daemon=True)
            t.start()
    sources = db.query(SkillSource).filter(
        SkillSource.is_hidden == False
    ).order_by(
        SkillSource.is_default.desc(), SkillSource.created_date.desc()
    ).all()
    return {"sources": [_source_to_dict(s) for s in sources]}


@router.post("/sources", status_code=201)
def add_source(req: AddSourceRequest, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.services.skill_source_service import detect_source_type, sync_source
    from app.models.skill_source import SkillSource

    existing = db.query(SkillSource).filter(SkillSource.url == req.url).first()
    if existing:
        raise HTTPException(409, detail={"message": "Source already registered", "source_id": existing.id})

    source_type = req.source_type or detect_source_type(req.url)
    name = req.name or req.url.split("//")[-1].split("/")[0]
    # `seed_curated_sources` defines the canonical defaults, but importing
    # it here would create a circular import on a cold start — the service
    # module pulls in `agent_browser_tool` lazily. Mirror the constants
    # instead.
    from app.services.skill_source_service import (
        DEFAULT_BRAND_COLOR, DEFAULT_ICON_EMOJI_PREFIX,
    )
    src = SkillSource(
        name=name,
        url=req.url,
        source_type=source_type,
        description=req.description,
        added_by=getattr(user, "id", None),
        is_default=False,
        last_sync_status="never",
        brand_color=req.brand_color or DEFAULT_BRAND_COLOR,
        icon_emoji=req.icon_emoji or (name[:1] or "?").upper(),
    )
    db.add(src)
    db.commit()
    db.refresh(src)

    # Kick off async sync in background. Pass None so the task gets its
    # own session — the request session is closed by the time the task
    # runs, which would silently drop the writes.
    try:
        asyncio.create_task(sync_source(src.id))
    except RuntimeError:
        pass  # No event loop — sync will happen on first manual trigger

    return _source_to_dict(src)


@router.delete("/sources/{source_id}")
def delete_source(
    source_id: str,
    force: bool = Query(False, description="Hard-delete a default source instead of hiding it. The URL is recorded as a tombstone so the seed won't re-create it on subsequent runs; the user can restore the source later via the API."),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user_required),
):
    from app.models.skill_source import SkillSource
    from app.models.external_skill import ExternalSkill
    from app.models.removed_curated_url import RemovedCuratedUrl
    src = db.query(SkillSource).filter(SkillSource.id == source_id).first()
    if not src:
        raise HTTPException(404, detail="Source not found")
    # Default sources get soft-deleted (is_hidden=True) by default — the
    # user can re-show them. ``force=true`` upgrades to a hard delete,
    # which fires the CASCADE on external_skills and removes the source
    # row entirely. For default sources, the URL is recorded as a
    # tombstone in ``removed_curated_urls`` so the seed won't
    # re-create the source on the next list call.
    if src.is_default and not force:
        src.is_hidden = True
        db.commit()
        return {"success": True, "hidden": True}
    # If hard-deleting a default source, record the tombstone first so
    # the seed can't undo our work. Idempotent — if the URL is already
    # tombstoned, the unique constraint on ``url`` is satisfied by the
    # SELECT.
    if src.is_default and force:
        existing_tombstone = db.query(RemovedCuratedUrl).filter(
            RemovedCuratedUrl.url == src.url
        ).first()
        if not existing_tombstone:
            db.add(RemovedCuratedUrl(
                url=src.url,
                removed_by=current_user.id if current_user else None,
            ))
    db.query(ExternalSkill).filter(ExternalSkill.source_id == source_id).delete()
    db.delete(src)
    db.commit()
    return {"success": True, "deleted": True}


@router.post("/sources/removed/{url:path}/restore", status_code=201)
def restore_removed_source(url: str, db: Session = Depends(get_db)):
    """Clear the tombstone for a previously-deleted curated source.

    The user deleted this source (hard delete, force=true), so its
    URL is in ``removed_curated_urls`` and the seed won't re-create
    it. Calling this endpoint clears the tombstone; the next list
    call re-creates the source from the seed definition. Returns
    404 if there's no tombstone for the given URL.
    """
    from urllib.parse import unquote
    from app.models.removed_curated_url import RemovedCuratedUrl
    from app.services.skill_source_service import seed_curated_sources
    decoded = unquote(url)
    tombstone = db.query(RemovedCuratedUrl).filter(RemovedCuratedUrl.url == decoded).first()
    if not tombstone:
        raise HTTPException(404, detail="No tombstone for this URL — source is not removed")
    db.delete(tombstone)
    db.commit()
    # Re-seed so the user doesn't need to call the list endpoint to
    # see the source come back. Returns the new source's row data.
    seed_curated_sources(db)
    from app.models.skill_source import SkillSource
    restored = db.query(SkillSource).filter(SkillSource.url == decoded).first()
    if not restored:
        # The URL isn't in CURATED_SOURCES (the user hard-deleted a
        # non-curated source that we somehow tombstoned — shouldn't
        # happen in practice, but handle gracefully).
        raise HTTPException(404, detail="URL was tombstoned but is not in curated sources list")
    return _source_to_dict(restored)


@router.get("/sources/removed")
def list_removed_sources(db: Session = Depends(get_db)):
    """List URLs the user has explicitly removed via hard-delete.

    The user can use this list with the ``/restore`` endpoint to
    bring a previously-deleted curated source back. The marketplace
    tab surfaces a "Show removed (N)" toggle in the UI so the user
    can see what's been removed and click to restore.
    """
    from app.models.removed_curated_url import RemovedCuratedUrl
    rows = db.query(RemovedCuratedUrl).order_by(RemovedCuratedUrl.removed_at.desc()).all()
    return {
        "removed": [
            {
                "url": r.url,
                "removed_at": r.removed_at.isoformat() if r.removed_at else None,
                "removed_by": r.removed_by,
            }
            for r in rows
        ]
    }


@router.post("/sources/{source_id}/sync", status_code=202)
async def sync_source_endpoint(source_id: str, db: Session = Depends(get_db)):
    from app.services.skill_source_service import sync_source
    result = await sync_source(source_id, db)
    return {"status": "completed", **result}


@router.get("/sources/{source_id}/skills")
def list_source_skills(
    source_id: str,
    q: Optional[str] = None,
    sort: str = "name",
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    from app.models.external_skill import ExternalSkill
    query = db.query(ExternalSkill).filter(
        ExternalSkill.source_id == source_id,
        ExternalSkill.is_deleted == False,
    )
    if q:
        query = query.filter(
            ExternalSkill.name.ilike(f"%{q}%") | ExternalSkill.description.ilike(f"%{q}%")
        )
    if sort == "popular":
        query = query.order_by(ExternalSkill.install_count.desc())
    else:
        query = query.order_by(ExternalSkill.name.asc())
    total = query.count()
    skills = query.offset(offset).limit(limit).all()
    return {"skills": [_external_skill_to_dict(s) for s in skills], "count": total}


# ─── External Skill Detail + Install ────────────────────────────────────

@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, db: Session = Depends(get_db)):
    from app.models.external_skill import ExternalSkill
    sk = db.query(ExternalSkill).filter(ExternalSkill.id == skill_id).first()
    if not sk:
        raise HTTPException(404, detail="Skill not found")
    d = _external_skill_to_dict(sk)
    d["skill_md"] = sk.skill_md
    return d


@router.post("/skills/{skill_id}/install")
def install_skill(skill_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.models.external_skill import ExternalSkill
    from app.models.tool import Tool
    from app.services.skill_sync import write_skill_md, reload_skills_registry

    sk = db.query(ExternalSkill).filter(ExternalSkill.id == skill_id).first()
    if not sk:
        raise HTTPException(404, detail="Skill not found")

    user_id = getattr(user, "id", None)
    existing = db.query(Tool).filter(
        Tool.name == sk.name,
        Tool.source == "external",
        Tool.created_by_id == user_id,
    ).first()
    if existing:
        return {"success": True, "already_installed": True, "tool_id": existing.id, "skill_name": sk.name}

    write_skill_md(
        name=sk.name,
        description=sk.description,
        body=sk.skill_md,
        category="marketplace",
        version=sk.version,
        author=sk.author or "external",
        tags=sk.tags or [],
    )
    tool = Tool(
        name=sk.name,
        description=sk.description,
        kind="system_skill",
        category="marketplace",
        source="external",
        github_url=sk.github_url,
        version=sk.version,
        publisher=sk.author,
        skill_md=sk.skill_md,
        enabled=True,
        status="active",
        created_by_id=user_id,
    )
    db.add(tool)
    sk.install_count = (sk.install_count or 0) + 1
    db.commit()
    db.refresh(tool)
    try:
        reload_skills_registry()
    except Exception:
        pass
    return {"success": True, "already_installed": False, "tool_id": tool.id, "skill_name": sk.name}


# ─── My Skills ──────────────────────────────────────────────────────────

@router.get("/my-skills")
def list_my_skills(db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.models.tool import Tool
    user_id = getattr(user, "id", None)
    tools = db.query(Tool).filter(
        Tool.source == "external",
        Tool.created_by_id == user_id,
        Tool.is_deleted == False,
    ).order_by(Tool.created_date.desc()).all()
    return {"skills": [_tool_to_dict(t) for t in tools]}


@router.delete("/my-skills/{tool_id}")
def remove_my_skill(tool_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    from app.models.tool import Tool
    from app.services.skill_sync import delete_skill_md, reload_skills_registry

    tool = db.query(Tool).filter(Tool.id == tool_id).first()
    if not tool:
        raise HTTPException(404, detail="Skill not found")
    user_id = getattr(user, "id", None)
    if tool.created_by_id != user_id:
        raise HTTPException(403, detail="Not your skill")

    delete_skill_md(tool.name, category="marketplace")
    tool.is_deleted = True
    db.commit()
    try:
        reload_skills_registry()
    except Exception:
        pass
    return {"success": True}


@router.get("/curated")
def list_curated():
    from app.services.skill_source_service import CURATED_SOURCES
    # Strip None values to keep the response shape tidy — the frontend can
    # fall back to its own defaults when fields are missing.
    sources = [
        {k: v for k, v in src.items() if v is not None}
        for src in CURATED_SOURCES
    ]
    return {"sources": sources}
