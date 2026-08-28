"""Generic entity router factory — registers all CRUD endpoints for any entity.

This is the core of the Base44 API compatibility layer. A single call to
register_entity_router() creates all 10 endpoints that the Base44 SDK expects.
"""

import logging
from typing import Type
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload

from app.database import Base
from app.deps import get_db, get_current_user_required
from app.services import entity_service

logger = logging.getLogger(__name__)

# KnowledgeBase connection fields whose change triggers catalog reindex.
# Changing name/description/status should NOT reindex.
_KB_CONNECTION_FIELDS = frozenset({
    "db_type", "host", "port", "database_name", "username", "password", "api_url",
})


def _maybe_fire_catalog_index(record) -> None:
    """Auto-trigger catalog discovery for a newly created database KnowledgeBase.

    Also drops the in-process schema cache for this KB so the agent never
    sees a stale table list after a new connection is saved.
    """
    try:
        from app.services.db.schema_service import invalidate_schema_cache
        invalidate_schema_cache(getattr(record, "id", None))
    except Exception:
        pass  # cache invalidation is best-effort
    try:
        from app.config import settings
        if not settings.SEMANTIC_CATALOG_ENABLED:
            return
        db_type = (getattr(record, "db_type", "") or "").lower()
        if db_type not in ("mysql", "postgres", "postgresql"):
            return
        from app.services.knowledge_graph.catalog_triggers import maybe_reindex_catalog_bg
        maybe_reindex_catalog_bg(record)
    except Exception:
        logger.warning("catalog auto-index on create failed", exc_info=True)


def _maybe_fire_catalog_index_on_update(prev, payload: dict, record) -> None:
    """Auto-trigger catalog discovery if KnowledgeBase connection fields changed.

    Also drops the in-process schema cache for this KB whenever connection
    fields change — the agent must not keep querying the old database.
    """
    try:
        from app.config import settings
        connection_changed = False
        if prev is not None:
            connection_changed = any(
                f in payload and payload.get(f) != getattr(prev, f, None)
                for f in _KB_CONNECTION_FIELDS
            )
        # Invalidate the schema cache on ANY connection-field change (even
        # when the semantic catalog is disabled or db_type is non-catalog).
        if connection_changed:
            from app.services.db.schema_service import invalidate_schema_cache
            invalidate_schema_cache(getattr(record, "id", None))
        if not settings.SEMANTIC_CATALOG_ENABLED:
            return
        db_type = (getattr(record, "db_type", "") or "").lower()
        if db_type not in ("mysql", "postgres", "postgresql"):
            return
        if prev is not None and not connection_changed:
            return
        from app.services.knowledge_graph.catalog_triggers import maybe_reindex_catalog_bg
        maybe_reindex_catalog_bg(record)
    except Exception:
        logger.warning("catalog auto-index on update failed", exc_info=True)


# Entities whose rows are owned by a single user and must be isolated per
# identity ("real software" data isolation). For these, list/get/update/
# delete are scoped to ``created_by_id == current identity`` — a real
# ``User.id`` when logged in.
#
# AgentApp + Project are user-scoped (plan 2026-07-27, fresh-user onboarding):
# a new user's MySpace is empty — they only see agents/projects they create.
# System agents (is_system=True) are still resolvable by the runtime via
# ensure_system_agents() (by name), NOT via these scoped list endpoints, so
# general_assistant auto-selection still works. Marketplace agents/skills live
# in separate tables (market_agent, marketplace_skills) and are unaffected.
# KnowledgeBase remains shared (global catalog).
#
# Shareable entities (plan 2026-08-03, multi-tenant RBAC) additionally
# include rows that were explicitly shared with the current user via
# ResourceShare — but only on the READ path.  Writes (update/delete)
# remain strictly owner-only.
#
# KnowledgeBase (2026-08-05): was previously a global catalog, leaking
# admin-created databases to every user's MySpace Connectors tab. Now
# user-scoped — each user only sees their own KBs. Project-scoped KB
# visibility (for shared projects) is handled separately by
# data_source_runtime._extend_with_project_kbs which queries the
# KnowledgeBase table directly, NOT through the generic entity API.
USER_SCOPED_ENTITIES = {
    "ChatSession",
    "ChatMessage",
    "AgentConversation",
    "AutomationTask",
    "AutomationExecution",
    "AutomationFile",
    "AgentApp",
    "Project",
    "KnowledgeBase",
    # User is self-scoped: the generic entity API may only read/update the
    # authenticated user's OWN record (filtered by id == caller id). This
    # closes the CRITICAL IDOR where any user could read/edit any account.
    # ``_filter_data`` additionally strips ``role`` so privilege escalation
    # via PUT /entities/User/{id} is impossible.
    "User",
}

_SHAREABLE_ENTITIES = {"Project", "AgentApp"}

# Pagination safety: the Base44 SDK defaults to ``limit=None`` (fetch all). On a
# large table that pulls every row into memory at once. Apply a sane default
# when the caller omits ``limit``, and clamp explicit limits to a hard max so a
# malicious/huge ``limit`` can't be used to exhaust server memory.
DEFAULT_LIST_LIMIT = 1000
MAX_LIST_LIMIT = 10000


def _owner_id(entity_name: str, user) -> str | None:
    """Return the ownership scope for a user-scoped entity, else None.

    ``user.id`` works for both a real ``User`` and the anonymous identity —
    both expose ``.id``. Returns None for non-scoped entities (shared) or
    when no identity is present (in which case scoped reads return nothing,
    which is the correct secure default).
    """
    if entity_name not in USER_SCOPED_ENTITIES:
        return None
    return getattr(user, "id", None)


def _include_shared(entity_name: str) -> bool:
    """Whether read paths should include shared-with-me rows."""
    return entity_name in _SHAREABLE_ENTITIES


def _resource_type_for_creator(user) -> str:
    """Derive resource_type from the creator's role (admin→'company', else 'personal')."""
    return "company" if getattr(user, "role", "user") == "admin" else "personal"


def _check_shareable_write(
    model_class, entity_name: str, record_id: str, user, db: Session,
) -> None:
    """Pre-check write access for shareable entities (Project, AgentApp).

    For shareable entities, if the record is visible to the caller via a
    ResourceShare but the caller is NOT the owner, raise 403 so the user
    gets a clear "view-only" message instead of a confusing 404.  If the
    record doesn't exist at all, raise 404.  For owner-writes and
    non-shareable entities, this is a pass-through.

    Org admins bypass this check — they always get ``can_edit=True``.
    """
    if not _include_shared(entity_name):
        return
    is_admin = getattr(user, "role", "user") == "admin"
    if is_admin:
        return
    visible = entity_service.get_record(
        model_class, record_id, db,
        owner_id=_owner_id(entity_name, user),
        include_shared=True,
    )
    if visible is None:
        raise HTTPException(status_code=404, detail=f"{entity_name} not found")
    if not visible.get("can_edit", True):
        raise HTTPException(
            status_code=403,
            detail="This resource was shared with you as view-only — "
                   "ask the owner to make changes or revoke the share.",
        )


def register_entity_router(
    entity_name: str,
    model_class: Type[Base],
) -> APIRouter:
    """Register all CRUD endpoints for an entity.

    Args:
        entity_name: Entity name as used in the URL (e.g. "ChatSession")
        model_class: SQLAlchemy model class for this entity

    Returns:
        APIRouter with all 10 endpoints registered
    """
    router = APIRouter(
        prefix=f"/apps/{{app_id}}/entities/{entity_name}",
        tags=[entity_name],
    )

    # --- GET / (list or filter) ---
    @router.get("")
    def list_or_filter(
        app_id: str,
        q: str | None = Query(None, description="JSON query filter"),
        sort: str | None = Query(None, description="Sort field, - prefix for desc"),
        limit: int | None = Query(None, description="Max records to return"),
        skip: int | None = Query(None, description="Records to skip"),
        db: Session = Depends(get_db),
        # Accept anonymous identities so unauthenticated browsing still works.
        # Write endpoints below still require real auth (get_current_user_required).
        user=Depends(get_current_user_required),
    ):
        owner = _owner_id(entity_name, user)
        is_admin = getattr(user, "role", "user") == "admin"
        # Cap unbounded reads: when the caller omits ``limit`` (Base44 SDK
        # default), apply a sane default so a giant table can't be pulled into
        # memory in one shot. Explicit limits are clamped to a hard max.
        effective_limit = limit if limit is not None else DEFAULT_LIST_LIMIT
        effective_limit = min(effective_limit, MAX_LIST_LIMIT)
        inc_shared = _include_shared(entity_name)
        if q:
            return entity_service.filter_records(
                model_class, db, q, sort, effective_limit, skip,
                owner_id=owner, include_shared=inc_shared,
                is_admin=is_admin,
            )
        return entity_service.list_records(
            model_class, db, sort, effective_limit, skip,
            owner_id=owner, include_shared=inc_shared,
            is_admin=is_admin,
        )

    # --- POST / (create) ---
    @router.post("")
    def create(
        app_id: str,
        data: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        created_by = user.id if user else None
        extra = {}
        if _include_shared(entity_name) and user:
            extra["resource_type"] = _resource_type_for_creator(user)
        result = entity_service.create_record(
            model_class, data, db, created_by,
            extra_fields=extra if extra else None,
        )
        # Auto-trigger catalog discovery for database KnowledgeBases
        if entity_name == "KnowledgeBase":
            _maybe_fire_catalog_index(result)
        return result

    # --- DELETE / (delete many by query) ---
    @router.delete("")
    def delete_many(
        app_id: str,
        query_data: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        count = entity_service.delete_many_records(model_class, query_data, db, owner_id=_owner_id(entity_name, user))
        return {"deleted_count": count}

    # --- POST /bulk (bulk create) ---
    @router.post("/bulk")
    def bulk_create(
        app_id: str,
        data_list: list,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        created_by = user.id if user else None
        return entity_service.bulk_create_records(model_class, data_list, db, created_by)

    # --- PUT /bulk (bulk update) ---
    @router.put("/bulk")
    def bulk_update(
        app_id: str,
        data_list: list,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        return entity_service.bulk_update_records(model_class, data_list, db, owner_id=_owner_id(entity_name, user))

    # --- PATCH /update-many (update many by query) ---
    @router.patch("/update-many")
    def update_many(
        app_id: str,
        body: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        query_data = body.get("query", {})
        update_data = body.get("data", {})
        count = entity_service.update_many_records(model_class, query_data, update_data, db, owner_id=_owner_id(entity_name, user))
        return {"updated_count": count}

    # --- PUT /{id}/restore (must be before /{id} routes) ---
    @router.put("/{record_id}/restore")
    def restore(
        app_id: str,
        record_id: str,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        result = entity_service.restore_record(model_class, record_id, db, owner_id=_owner_id(entity_name, user))
        if not result:
            raise HTTPException(status_code=404, detail=f"{entity_name} not found")
        return result

    # --- GET /{id} ---
    @router.get("/{record_id}")
    def get_by_id(
        app_id: str,
        record_id: str,
        db: Session = Depends(get_db),
        # Accept anonymous identities so unauthenticated browsing still works.
        user=Depends(get_current_user_required),
    ):
        is_admin = getattr(user, "role", "user") == "admin"
        result = entity_service.get_record(
            model_class, record_id, db,
            owner_id=_owner_id(entity_name, user),
            include_shared=_include_shared(entity_name),
            is_admin=is_admin,
        )
        if not result:
            raise HTTPException(status_code=404, detail=f"{entity_name} not found")
        return result

    # --- PUT /{id} ---
    @router.put("/{record_id}")
    def update_by_id(
        app_id: str,
        record_id: str,
        data: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        _check_shareable_write(model_class, entity_name, record_id, user, db)
        # Snapshot pre-update record for connection-field diff (KnowledgeBase only)
        prev = None
        if entity_name == "KnowledgeBase":
            prev = entity_service.get_record(model_class, record_id, db)
        # Tool supports "claim" semantics — the Skills tab UI sends
        # ``{ created_by_id: <user.id> }`` to mark a tool as added to
        # My Skills. Tool is not in USER_SCOPED_ENTITIES so owner_id is
        # None, but the claim helper still needs the caller's id. We
        # forward it as ``claim_user_id`` only for claimable entities.
        # (2026-08-28 Skills-tab fix — see entity_service docstring.)
        claim_user_id = getattr(user, "id", None) if entity_name == "Tool" else None
        result = entity_service.update_record(
            model_class, record_id, data, db,
            owner_id=_owner_id(entity_name, user),
            claim_user_id=claim_user_id,
        )
        if not result:
            raise HTTPException(status_code=404, detail=f"{entity_name} not found")
        # Auto-trigger catalog discovery if connection fields changed
        if entity_name == "KnowledgeBase":
            _maybe_fire_catalog_index_on_update(prev, data, result)
        return result

    # --- DELETE /{id} ---
    @router.delete("/{record_id}")
    def delete_by_id(
        app_id: str,
        record_id: str,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        _check_shareable_write(model_class, entity_name, record_id, user, db)
        success = entity_service.soft_delete_record(model_class, record_id, db, owner_id=_owner_id(entity_name, user))
        if not success:
            raise HTTPException(status_code=404, detail=f"{entity_name} not found")
        return {"deleted": True, "id": record_id}

    return router


def register_project_kb_router() -> APIRouter:
    """Register endpoint for listing KnowledgeBases bound to a project.

    Unlike the generic entity API (which is user-scoped and only returns
    the caller's own KBs), this endpoint lists ALL non-deleted KBs attached
    to a project — including admin-created KBs on a project shared with
    a non-admin user (project-bundle sharing).

    Access gate: the caller must be the project owner OR have an active
    ResourceShare granting them access to the project.
    """
    from app.models.project import Project
    from app.models.knowledge_base import KnowledgeBase
    from app.models.resource_share import ResourceShare

    router = APIRouter(
        prefix="/api/apps/{app_id}",
        tags=["knowledge-bases"],
    )

    @router.get("/projects/{project_id}/knowledge-bases")
    def list_project_kbs(
        app_id: str,
        project_id: str,
        sort: str | None = Query(None, description="Sort field, - prefix for desc"),
        limit: int | None = Query(None, description="Max records to return"),
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        # 1. Verify project exists
        project = db.query(Project).filter(
            Project.id == project_id,
            Project.is_deleted == False,
        ).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        # 2. Access gate: owner OR shared via ResourceShare OR org admin
        is_admin = getattr(user, "role", "user") == "admin"
        has_access = is_admin or project.created_by_id == user.id
        if not has_access:
            share = db.query(ResourceShare).filter(
                ResourceShare.resource_id == project_id,
                ResourceShare.resource_type == "project",
                ResourceShare.shared_with_user_id == user.id,
                ResourceShare.is_deleted == False,
            ).first()
            has_access = share is not None

        if not has_access:
            raise HTTPException(
                status_code=403,
                detail="You do not have access to this project",
            )

        # 3. Query non-deleted KBs bound to this project.
        #    Match by exact FK (project_id) OR by legacy project name.
        from sqlalchemy import or_
        q = db.query(KnowledgeBase).filter(
            or_(
                KnowledgeBase.project_id == project_id,
                KnowledgeBase.project == project.name,
            ),
            KnowledgeBase.is_deleted == False,
        )

        # 4. Apply tenant filter
        org_filter = entity_service._tenant_filters(KnowledgeBase)
        for cond in org_filter:
            q = q.filter(cond)

        # 5. Sort (default: newest first)
        from app.utils.sort_parser import parse_sort
        order_clauses = parse_sort(KnowledgeBase, sort)
        q = q.order_by(*order_clauses)

        # 6. Limit
        effective_limit = limit if limit is not None else 200
        effective_limit = max(1, min(effective_limit, 500))
        q = q.limit(effective_limit)

        kbs = q.all()

        # 7. Annotate with can_edit for frontend permissions
        return [
            {
                **kb.to_dict(),
                "can_edit": kb.created_by_id == user.id,
                "is_shared_with_me": kb.created_by_id != user.id,
            }
            for kb in kbs
        ]

    return router
