"""Agent Studio router — manifest, bindings, preflight, and dry-run APIs.

Provides:
* GET /api/agent-studio/{agent_id}/preflight — run preflight checks
* POST /api/agent-studio/{agent_id}/dry-run — dry-run with test message
* GET /api/agent-studio/{agent_id}/bindings — list data + skill bindings
* POST /api/agent-studio/{agent_id}/bindings/data — add data binding
* POST /api/agent-studio/{agent_id}/bindings/skill — add skill binding

Security: every endpoint requires an authenticated user.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-studio"])


class DataBindingRequest(BaseModel):
    datasource_id: str
    access_mode: str = "read_only"
    allowed_tables: Optional[list] = None
    allowed_columns: Optional[dict] = None
    nl2sql_enabled: bool = True


class SkillBindingRequest(BaseModel):
    skill_name: str
    skill_version: Optional[str] = None
    is_allowed: bool = True
    is_pinned: bool = False


class DataBindingUpdateRequest(BaseModel):
    is_enabled: bool


@router.get("/agent-studio/{agent_id}/preflight")
def run_preflight(
    agent_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Run preflight readiness checks on an agent."""
    from app.services.agent_studio.preflight import PreflightService
    service = PreflightService(db)
    return service.check_agent(agent_id)


@router.post("/agent-studio/{agent_id}/dry-run")
def dry_run(
    agent_id: str,
    test_message: str = "Hello, what can you do?",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Perform a dry-run of the agent."""
    from app.services.agent_studio.preflight import PreflightService
    service = PreflightService(db)
    return service.dry_run(agent_id, test_message)


def _binding_dict(b) -> dict:
    return {
        "id": b.id, "agent_app_id": b.agent_app_id,
        "datasource_id": b.datasource_id, "access_mode": b.access_mode,
        "allowed_tables": b.allowed_tables,
        "allowed_columns": b.allowed_columns,
        "blocked_tables": b.blocked_tables, "row_filters": b.row_filters,
        "nl2sql_enabled": bool(b.nl2sql_enabled),
        "is_enabled": bool(b.is_enabled),
        "is_deleted": bool(b.is_deleted),
    }


def _service_error(exc: Exception) -> HTTPException:
    """Map binding-service domain errors onto HTTP statuses (fail-closed)."""
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/agent-studio/{agent_id}/bindings")
def get_bindings(
    agent_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List data and skill bindings for an agent (org-scoped)."""
    from app.models.agent_skill_binding import AgentSkillBinding
    from app.services.agent_binding_service import list_bindings

    try:
        data_bindings = list_bindings(db, user.org_id, agent_id)
    except (PermissionError, LookupError) as exc:
        raise _service_error(exc)
    skill_bindings = (
        db.query(AgentSkillBinding)
        .filter(AgentSkillBinding.agent_app_id == agent_id, AgentSkillBinding.is_deleted == False)  # noqa: E712
        .all()
    )

    return {
        "data_bindings": [_binding_dict(b) for b in data_bindings],
        "skill_bindings": [b.to_dict() for b in skill_bindings],
    }


@router.post("/agent-studio/{agent_id}/bindings/data")
def add_data_binding(
    agent_id: str,
    req: DataBindingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Add (or idempotently update) a data binding via the binding service.

    Phase 8 (ADR-1): the service is the sole writer — org wall, duplicate
    handling, and the AgentApp.knowledge_bases compatibility projection all
    happen inside it."""
    from app.services.agent_binding_service import upsert_binding

    try:
        binding = upsert_binding(
            db, user.org_id, agent_id, req.datasource_id,
            actor_id=user.id, access_mode=req.access_mode,
            allowed_tables=req.allowed_tables,
            allowed_columns=req.allowed_columns,
            nl2sql_enabled=req.nl2sql_enabled,
        )
    except (PermissionError, LookupError) as exc:
        raise _service_error(exc)

    # Project-scoped KB ingestion trigger (flag-gated; the default BI
    # agent that used it was removed 2026-08-27 — PROJECT_KNOWLEDGE_AGENT_NAMES
    # is empty so this block is inert unless an app opts in by name).
    try:
        from app.config import settings
        from app.models.agent_app import AgentApp
        from app.models.knowledge_base import KnowledgeBase
        if getattr(settings, "PROJECT_KNOWLEDGE_CACHE_ENABLED", False):
            agent_names = getattr(settings, "PROJECT_KNOWLEDGE_AGENT_NAMES", []) or []
            agent_row = db.query(AgentApp).filter(
                AgentApp.id == agent_id, AgentApp.is_deleted == False,  # noqa: E712
            ).first()
            if agent_row and agent_row.name in agent_names:
                # The datasource may be a KnowledgeBase or a regular datasource.
                # The cache's ingest step uses catalog_indexer.index_kb_catalog which
                # works on KB rows; for non-KB bindings we just skip the trigger.
                kb = db.query(KnowledgeBase).filter(
                    KnowledgeBase.id == req.datasource_id,
                    KnowledgeBase.is_deleted == False,  # noqa: E712
                ).first()
                if kb is not None and getattr(agent_row, "project_id", None):
                    import asyncio
                    from app.services.project_knowledge.ingestion import ingest_for_project
                    project_id = agent_row.project_id
                    kb_id = kb.id
                    async def _bg():
                        from app.database import SessionLocal
                        bg_db = SessionLocal()
                        try:
                            await ingest_for_project(project_id, kb_id, bg_db)
                        except Exception as _e:
                            logger.warning("project_knowledge bg ingest failed: %s", _e)
                        finally:
                            try:
                                bg_db.close()
                            except Exception:
                                pass
                    try:
                        loop = asyncio.get_event_loop()
                        loop.create_task(_bg())
                    except RuntimeError:
                        # No running loop (e.g. sync test) -- run inline.
                        asyncio.run(_bg())
    except Exception as _trig_err:
        logger.warning("project_knowledge trigger skipped (non-fatal): %s", _trig_err)

    return _binding_dict(binding)


@router.patch("/agent-studio/{agent_id}/bindings/data/{binding_id}")
def update_data_binding(
    agent_id: str,
    binding_id: str,
    req: DataBindingUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Enable/disable a binding (revocation-lite; the row survives)."""
    from app.services.agent_binding_service import set_binding_enabled

    try:
        set_binding_enabled(db, user.org_id, binding_id,
                            bool(req.is_enabled), actor_id=user.id)
    except (PermissionError, LookupError) as exc:
        raise _service_error(exc)
    from app.models.agent_data_binding import AgentDataBinding
    return _binding_dict(db.get(AgentDataBinding, binding_id))


@router.delete("/agent-studio/{agent_id}/bindings/data/{binding_id}")
def remove_data_binding(
    agent_id: str,
    binding_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Revoke a binding (soft delete; effective on the agent's next turn)."""
    from app.services.agent_binding_service import revoke_binding

    try:
        revoke_binding(db, user.org_id, binding_id, actor_id=user.id)
    except (PermissionError, LookupError) as exc:
        raise _service_error(exc)
    from app.models.agent_data_binding import AgentDataBinding
    return _binding_dict(db.get(AgentDataBinding, binding_id))


@router.get("/agent-studio/{agent_id}/source-descriptors")
def get_source_descriptors(
    agent_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Structured runtime source descriptors (Phase 8 ADR-3).

    One contract for the UI: what this agent MAY access, its binding state,
    type, and readiness. Org-scoped and fail-closed; a foreign-org agent
    yields an empty list, never foreign rows."""
    from app.models.knowledge_base import KnowledgeBase
    from app.services.agent_binding_service import list_bindings

    try:
        bindings = list_bindings(db, user.org_id, agent_id)
    except (PermissionError, LookupError) as exc:
        raise _service_error(exc)

    descriptors = []
    for b in bindings:
        ds = db.get(KnowledgeBase, b.datasource_id)
        if ds is None or ds.is_deleted:
            descriptors.append({
                "id": b.datasource_id, "name": b.datasource_id,
                "resource_type": "unknown", "source_kind": "unknown",
                "db_type": None, "binding_id": b.id,
                "access_mode": b.access_mode,
                "is_enabled": bool(b.is_enabled),
                "readiness": "unavailable (resource deleted)",
                "queryable": False,
            })
            continue
        is_db = (ds.source_kind or "database") == "database"
        descriptors.append({
            "id": ds.id, "name": ds.name,
            "resource_type": "database" if is_db else "knowledge_base",
            "source_kind": ds.source_kind or "database",
            "db_type": getattr(ds, "db_type", None),
            "binding_id": b.id, "access_mode": b.access_mode,
            "is_enabled": bool(b.is_enabled),
            "readiness": getattr(ds, "readiness_note", None) or "ready",
            "queryable": bool(is_db and b.nl2sql_enabled and b.is_enabled),
        })
    return {"descriptors": descriptors}


@router.post("/agent-studio/{agent_id}/bindings/skill")
def add_skill_binding(
    agent_id: str,
    req: SkillBindingRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Add a skill binding to an agent."""
    from app.models.agent_skill_binding import AgentSkillBinding
    from uuid import uuid4

    binding = AgentSkillBinding(
        id=str(uuid4()),
        agent_app_id=agent_id,
        skill_name=req.skill_name,
        skill_version=req.skill_version,
        is_allowed=req.is_allowed,
        is_pinned=req.is_pinned,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding.to_dict()
