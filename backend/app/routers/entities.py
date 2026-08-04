"""Generic entity router factory — registers all CRUD endpoints for any entity.

This is the core of the Base44 API compatibility layer. A single call to
register_entity_router() creates all 10 endpoints that the Base44 SDK expects.
"""

from typing import Type
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import Base
from app.deps import get_db, get_current_user_required
from app.services import entity_service


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
USER_SCOPED_ENTITIES = {
    "ChatSession",
    "ChatMessage",
    "AgentConversation",
    "AutomationTask",
    "AutomationExecution",
    "AutomationFile",
    "AgentApp",
    "Project",
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
    """
    if not _include_shared(entity_name):
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
    async def list_or_filter(
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
            )
        return entity_service.list_records(
            model_class, db, sort, effective_limit, skip,
            owner_id=owner, include_shared=inc_shared,
        )

    # --- POST / (create) ---
    @router.post("")
    async def create(
        app_id: str,
        data: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        created_by = user.id if user else None
        extra = {}
        if _include_shared(entity_name) and user:
            extra["resource_type"] = _resource_type_for_creator(user)
        return entity_service.create_record(
            model_class, data, db, created_by,
            extra_fields=extra if extra else None,
        )

    # --- DELETE / (delete many by query) ---
    @router.delete("")
    async def delete_many(
        app_id: str,
        query_data: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        count = entity_service.delete_many_records(model_class, query_data, db, owner_id=_owner_id(entity_name, user))
        return {"deleted_count": count}

    # --- POST /bulk (bulk create) ---
    @router.post("/bulk")
    async def bulk_create(
        app_id: str,
        data_list: list,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        created_by = user.id if user else None
        return entity_service.bulk_create_records(model_class, data_list, db, created_by)

    # --- PUT /bulk (bulk update) ---
    @router.put("/bulk")
    async def bulk_update(
        app_id: str,
        data_list: list,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        return entity_service.bulk_update_records(model_class, data_list, db, owner_id=_owner_id(entity_name, user))

    # --- PATCH /update-many (update many by query) ---
    @router.patch("/update-many")
    async def update_many(
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
    async def restore(
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
    async def get_by_id(
        app_id: str,
        record_id: str,
        db: Session = Depends(get_db),
        # Accept anonymous identities so unauthenticated browsing still works.
        user=Depends(get_current_user_required),
    ):
        result = entity_service.get_record(
            model_class, record_id, db,
            owner_id=_owner_id(entity_name, user),
            include_shared=_include_shared(entity_name),
        )
        if not result:
            raise HTTPException(status_code=404, detail=f"{entity_name} not found")
        return result

    # --- PUT /{id} ---
    @router.put("/{record_id}")
    async def update_by_id(
        app_id: str,
        record_id: str,
        data: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        _check_shareable_write(model_class, entity_name, record_id, user, db)
        result = entity_service.update_record(model_class, record_id, data, db, owner_id=_owner_id(entity_name, user))
        if not result:
            raise HTTPException(status_code=404, detail=f"{entity_name} not found")
        return result

    # --- DELETE /{id} ---
    @router.delete("/{record_id}")
    async def delete_by_id(
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
