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

    IMPORTANT: ``record`` is the dict returned by
    ``entity_service.create_record`` (``record.to_dict()``), NOT the ORM row.
    Reading ``getattr(record, "db_type", "")`` off a dict silently yields ``""``,
    which made this hook a no-op for EVERY KB created through the generic entity
    API — the UI only appeared to work because KbSetupDialog calls
    /catalog/reindex itself. All field access goes through
    ``catalog_triggers.kb_field`` and the dialect gate through
    ``catalog_triggers.supports_catalog`` so both shapes work and there is one
    place that decides which dialects are catalogable.
    """
    from app.services.knowledge_graph.catalog_triggers import kb_field
    if not kb_field(record, "id"):
        # No id → nothing to index (and nothing to cache-invalidate). Defensive:
        # a payload without an id would otherwise key the per-KB lock on None.
        return
    try:
        from app.services.db.schema_service import invalidate_schema_cache
        invalidate_schema_cache(kb_field(record, "id"))
    except Exception:
        pass  # cache invalidation is best-effort
    try:
        from app.services.knowledge_graph.catalog_triggers import (
            is_catalogable_record,
            maybe_reindex_catalog_bg,
        )
        if not is_catalogable_record(record):
            return
        maybe_reindex_catalog_bg(record)
    except Exception:
        logger.warning("catalog auto-index on create failed", exc_info=True)


def _maybe_fire_catalog_index_on_update(prev, payload: dict, record) -> None:
    """Auto-trigger catalog discovery if KnowledgeBase connection fields changed.

    Also drops the in-process schema cache for this KB whenever connection
    fields change — the agent must not keep querying the old database.

    ``prev`` and ``record`` are ``to_dict()`` dicts — see the create hook note
    about shape-agnostic field access.
    """
    from app.services.knowledge_graph.catalog_triggers import kb_field
    try:
        from app.config import settings
        connection_changed = False
        if prev is not None:
            connection_changed = any(
                f in payload and payload.get(f) != getattr(prev, f, None)
                for f in _KB_CONNECTION_FIELDS
            )
        # Invalidate the schema cache on ANY connection-field change (even
        # when the semantic catalog is disabled or the dialect isn't catalogable).
        if connection_changed:
            from app.services.db.schema_service import invalidate_schema_cache
            invalidate_schema_cache(kb_field(record, "id"))
        if not settings.SEMANTIC_CATALOG_ENABLED:
            return
        from app.services.knowledge_graph.catalog_triggers import (
            is_catalogable_record,
            maybe_reindex_catalog_bg,
        )
        if not is_catalogable_record(record):
            return
        if prev is not None and not connection_changed:
            return
        maybe_reindex_catalog_bg(record)
    except Exception:
        logger.warning("catalog auto-index on update failed", exc_info=True)


# ── AutomationTask schedule normalization ────────────────────────────────────
# AutomationTask rows store cron_expression + next_run_at *derived from* the
# free-form ``schedule`` string. Only the chat create tool (_create_automation)
# and the /toggle resume path ever parsed; generic entity CRUD and seed.py
# wrote the raw schedule and left cron_expression='' / next_run_at=NULL, so the
# dispatcher (which fires ONLY status=="active" AND next_run_at IS NOT NULL)
# never ran those tasks — silently. Mirror the chat tool's semantics here on
# every generic create/update so NO entry point can persist a dead task.
_AUTOMATION_MANUAL_LIKE = {"manual", "none", "on-demand", "on demand", "run manually", "run-on-demand"}


def _normalize_automation_schedule_payload(
    data: dict, current=None, *, creating: bool
) -> None:
    """Merge normalized schedule fields into an AutomationTask payload in place.

    Semantics (parity with ``agent_tools._create_automation``):

    * A ``schedule`` that is present and not manual-like is parsed to cron.
      Unparseable schedules raise HTTP 400 — a task with a broken schedule is
      exactly the silent-never-fires state this hook exists to prevent, and an
      update that would persist it is rejected loudly instead.
    * ``cron_expression`` is set from the parse; ``timezone`` falls back to the
      existing row's value or UTC; ``next_run_at`` is computed when a cron
      exists (the dispatcher only fires tasks with next_run_at).
    * On create with a parseable schedule and no explicit status, status
      defaults to ``active`` so the new task actually runs (tool parity).
    * On update, the schedule is only re-parsed when it changed OR the row has
      no cron_expression yet (dead-task repair). Touching name/description on
      an already-scheduled task must not rewrite its next_run_at.
    """
    from app.models.automation_task import AutomationTask
    from app.services.schedule_parser import (
        ScheduleParseError,
        next_run_at as _next_run,
        parse_schedule,
    )

    schedule = (data.get("schedule") or "").strip()
    is_manual = (schedule.lower() in _AUTOMATION_MANUAL_LIKE) or not schedule
    if is_manual:
        # Manual/absent schedule → nothing to schedule. On create, force the
        # canonical paused default so the row can't sit "active" with no cron
        # (active-without-next_run_at is the dead state the dispatcher skips).
        if creating and not data.get("status"):
            data["status"] = "paused"
        return

    current_schedule = ((current or {}).get("schedule") or "").strip()
    current_cron = (current or {}).get("cron_expression") or ""
    if (not creating) and schedule == current_schedule and current_cron:
        return  # schedule unchanged and already normalized — nothing to do

    try:
        cron = parse_schedule(schedule)
    except ScheduleParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — never persist a half-parsed schedule
        logger.warning("AutomationTask schedule parse failed (%r): %s", schedule, e)
        raise HTTPException(status_code=400, detail=f"Could not parse schedule: {e}")

    tz_name = (data.get("timezone") or "").strip() or (current or {}).get("timezone") or "UTC"
    data["cron_expression"] = cron
    data["timezone"] = tz_name
    data["next_run_at"] = _next_run(cron, tz_name=tz_name).isoformat()
    if creating and not data.get("status"):
        data["status"] = "active"
    # Invalid/legacy statuses ('running' etc.) must not be persisted — coerce to
    # the canonical default, exactly like the chat tool (see VALID_STATUSES).
    if data.get("status") and data["status"] not in AutomationTask.VALID_STATUSES:
        data["status"] = "active" if cron else "paused"


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


def _tenant_ids(user, app_id_url: str | None = None) -> tuple[str, str]:
    """Resolve the authenticated tenant (org, app) for entity scoping.

    2026-10-22 (audit P1: entity API tenant scoping hardcoded ``default-org``):
    every entity read/write is now scoped to the *authenticated* principal's
    org/app instead of the platform default, so a tenant can never read or
    write another tenant's rows through the generic CRUD API. Single-tenant
    (default-org) deployments resolve to the default, so behavior is
    unchanged there. The client-supplied ``app_id`` in the URL is only a
    fallback when the user has no app binding — the auth principal wins.
    """
    from app.config import settings
    org = getattr(user, "org_id", None) or settings.DEFAULT_ORG_ID
    app = getattr(user, "app_id", None) or app_id_url or settings.DEFAULT_APP_ID
    return org, app


def _resource_type_for_creator(user) -> str:
    """Derive resource_type from the creator's role (admin→'company', else 'personal')."""
    return "company" if getattr(user, "role", "user") == "admin" else "personal"


def _check_shareable_write(
    model_class, entity_name: str, record_id: str, user, db: Session,
    org_id: str | None = None, app_id: str | None = None,
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
        org_id=org_id, app_id=app_id,
    )
    if visible is None:
        raise HTTPException(status_code=404, detail=f"{entity_name} not found")
    if not visible.get("can_edit", True):
        raise HTTPException(
            status_code=403,
            detail="This resource was shared with you as view-only — "
                   "ask the owner to make changes or revoke the share.",
        )


# Phase 0B (2026-11-01): entity types whose deletion triggers enterprise
# orphan cleanup (app/services/enterprise/cleanup.py). Phase 7C (D4):
# AgentApp deletion now cleans its AgentDataBinding rows too.
_ENTITY_RESOURCE_TYPES = {
    "KnowledgeBase": "knowledge_base",
    "McpServer": "mcp_server",
    "AutomationTask": "automation_task",
    "AgentApp": "agent_app",
}


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
        fields: str | None = Query(
            None,
            description="Comma-separated column subset to return (list view), "
                        "e.g. fields=id,name,description. Heavy body columns "
                        "(e.g. Tool.skill_md) are excluded unless requested.",
        ),
        db: Session = Depends(get_db),
        # Accept anonymous identities so unauthenticated browsing still works.
        # Write endpoints below still require real auth (get_current_user_required).
        user=Depends(get_current_user_required),
    ):
        owner = _owner_id(entity_name, user)
        is_admin = getattr(user, "role", "user") == "admin"
        org_id, app_id = _tenant_ids(user, app_id)
        # Cap unbounded reads: when the caller omits ``limit`` (Base44 SDK
        # default), apply a sane default so a giant table can't be pulled into
        # memory in one shot. Explicit limits are clamped to a hard max.
        effective_limit = limit if limit is not None else DEFAULT_LIST_LIMIT
        effective_limit = min(effective_limit, MAX_LIST_LIMIT)
        field_list = (
            [f.strip() for f in fields.split(",") if f.strip()]
            if fields else None
        )
        inc_shared = _include_shared(entity_name)
        if q:
            return entity_service.filter_records(
                model_class, db, q, sort, effective_limit, skip,
                owner_id=owner, include_shared=inc_shared,
                is_admin=is_admin, fields=field_list,
                org_id=org_id, app_id=app_id,
            )
        return entity_service.list_records(
            model_class, db, sort, effective_limit, skip,
            owner_id=owner, include_shared=inc_shared,
            is_admin=is_admin, fields=field_list,
            org_id=org_id, app_id=app_id,
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
        org_id, app_id = _tenant_ids(user, app_id)
        extra = {}
        if _include_shared(entity_name) and user:
            extra["resource_type"] = _resource_type_for_creator(user)
        if entity_name == "AutomationTask":
            # Parse schedule → cron_expression/next_run_at BEFORE insert so a
            # dead (never-firing) task can't be persisted through the generic
            # API. Raises 400 on an unparseable schedule.
            _normalize_automation_schedule_payload(data, creating=True)
        result = entity_service.create_record(
            model_class, data, db, created_by,
            extra_fields=extra if extra else None,
            org_id=org_id, app_id=app_id,
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
        org_id, app_id = _tenant_ids(user, app_id)
        resource_type = _ENTITY_RESOURCE_TYPES.get(entity_name)
        if resource_type:
            # Phase 7C (D3): bulk delete of enterprise resources must run
            # the same orphan cleanup as single delete — per record,
            # fail-open, honestly surfaced.
            from app.services.entity_service import soft_delete_many_records
            deleted_ids = soft_delete_many_records(
                model_class, query_data, db,
                owner_id=_owner_id(entity_name, user),
                org_id=org_id, app_id=app_id,
            )
            cleanup_results = []
            for rid in deleted_ids:
                try:
                    from app.services.enterprise.cleanup import (
                        handle_resource_deleted)
                    cleanup_results.append({
                        "id": rid,
                        **(handle_resource_deleted(
                            db, resource_type=resource_type,
                            resource_id=rid, org_id=org_id,
                            actor_id=getattr(user, "id", None),
                        ) or {}),
                    })
                except LookupError:
                    cleanup_results.append({"id": rid})
                except Exception as exc:  # noqa: BLE001 — surface, don't undo
                    logger.exception(
                        "bulk-delete cleanup failed for %s %s (org %s)",
                        entity_name, rid, org_id)
                    cleanup_results.append(
                        {"id": rid, "cleanup_error": str(exc)[:300]})
            return {"deleted_count": len(deleted_ids),
                    "cleanup_results": cleanup_results}
        count = entity_service.delete_many_records(
            model_class, query_data, db, owner_id=_owner_id(entity_name, user),
            org_id=org_id, app_id=app_id,
        )
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
        org_id, app_id = _tenant_ids(user, app_id)
        return entity_service.bulk_create_records(
            model_class, data_list, db, created_by,
            org_id=org_id, app_id=app_id,
        )

    # --- PUT /bulk (bulk update) ---
    @router.put("/bulk")
    def bulk_update(
        app_id: str,
        data_list: list,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        org_id, app_id = _tenant_ids(user, app_id)
        return entity_service.bulk_update_records(
            model_class, data_list, db, owner_id=_owner_id(entity_name, user),
            org_id=org_id, app_id=app_id,
        )

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
        org_id, app_id = _tenant_ids(user, app_id)
        count = entity_service.update_many_records(
            model_class, query_data, update_data, db,
            owner_id=_owner_id(entity_name, user),
            org_id=org_id, app_id=app_id,
        )
        return {"updated_count": count}

    # --- PUT /{id}/restore (must be before /{id} routes) ---
    @router.put("/{record_id}/restore")
    def restore(
        app_id: str,
        record_id: str,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        org_id, app_id = _tenant_ids(user, app_id)
        result = entity_service.restore_record(
            model_class, record_id, db, owner_id=_owner_id(entity_name, user),
            org_id=org_id, app_id=app_id,
        )
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
        org_id, app_id = _tenant_ids(user, app_id)
        result = entity_service.get_record(
            model_class, record_id, db,
            owner_id=_owner_id(entity_name, user),
            include_shared=_include_shared(entity_name),
            is_admin=is_admin,
            org_id=org_id, app_id=app_id,
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
        org_id, app_id = _tenant_ids(user, app_id)
        _check_shareable_write(model_class, entity_name, record_id, user, db,
                               org_id=org_id, app_id=app_id)
        # Snapshot pre-update record for diff-driven hooks (KnowledgeBase
        # catalog reindex; AutomationTask schedule re-parse). Owner-scoped so a
        # caller can't probe another user's row through the hook.
        prev = None
        if entity_name in ("KnowledgeBase", "AutomationTask"):
            prev = entity_service.get_record(
                model_class, record_id, db,
                owner_id=_owner_id(entity_name, user),
                org_id=org_id, app_id=app_id,
            )
        if entity_name == "AutomationTask" and prev is not None:
            # Re-parse when schedule/timezone changed or cron_expression is
            # still empty (dead-task repair). This is what makes detail-page
            # schedule edits actually take effect — the generic PUT is the only
            # write path that page uses (it never hit the chat tool's parser).
            _normalize_automation_schedule_payload(data, current=prev, creating=False)
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
            org_id=org_id, app_id=app_id,
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
        org_id, app_id = _tenant_ids(user, app_id)
        _check_shareable_write(model_class, entity_name, record_id, user, db,
                               org_id=org_id, app_id=app_id)
        success = entity_service.soft_delete_record(
            model_class, record_id, db, owner_id=_owner_id(entity_name, user),
            org_id=org_id, app_id=app_id,
        )
        if not success:
            raise HTTPException(status_code=404, detail=f"{entity_name} not found")
        # Phase 0B: orphan cleanup for enterprise resource types — runs
        # AFTER authorization + soft-delete, org-scoped, idempotent. A
        # cleanup failure is surfaced (never reported as clean success)
        # but does not undo the caller-authorized deletion.
        resource_type = _ENTITY_RESOURCE_TYPES.get(entity_name)
        cleanup_payload: dict | None = None
        if resource_type:
            try:
                from app.services.enterprise.cleanup import handle_resource_deleted
                cleanup_payload = handle_resource_deleted(
                    db, resource_type=resource_type, resource_id=record_id,
                    org_id=org_id, actor_id=getattr(user, "id", None),
                )
            except LookupError:
                pass  # nothing to clean (defensive; soft-delete already verified)
            except Exception as exc:  # noqa: BLE001 — surface, don't undo
                logger.exception(
                    "post-delete cleanup failed for %s %s (org %s)",
                    entity_name, record_id, org_id)
                cleanup_payload = {"cleanup_error": str(exc)[:300]}
        response: dict = {"deleted": True, "id": record_id}
        if cleanup_payload is not None:
            response["cleanup"] = cleanup_payload
        return response

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
        #    Home project (project_id) OR legacy project name OR an explicit
        #    link row (multi-project sharing, 2026-10-31) — one shared filter.
        from app.services.project_kb_membership import project_kb_membership_filter
        q = db.query(KnowledgeBase).filter(
            project_kb_membership_filter(project),
            KnowledgeBase.is_deleted == False,
        )

        # 4. Apply tenant filter — scope to the authenticated principal's org,
        #    not the hardcoded platform default (2026-10-22 audit P1 fix).
        org_filter = entity_service._tenant_filters(
            KnowledgeBase,
            org_id=getattr(user, "org_id", None),
            app_id=getattr(user, "app_id", None),
        )
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

    @router.post("/projects/{project_id}/knowledge-bases/{kb_id}")
    def attach_kb_to_project_endpoint(
        app_id: str,
        project_id: str,
        kb_id: str,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        """Attach an existing KB to a project (multi-project sharing).

        ADDITIVE: creates a link row; never steals the KB from another
        project (the old 1-to-1 overwrite). Idempotent — re-attach is a
        no-op, not an error.
        """
        from app.services.project_kb_membership import attach_kb_to_project

        project = db.query(Project).filter(
            Project.id == project_id,
            Project.is_deleted == False,
        ).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        ).first()
        if not kb:
            raise HTTPException(status_code=404, detail="KnowledgeBase not found")

        # Tenant guard: caller's org must match the KB's org (fail-closed).
        kb_org = getattr(kb, "org_id", None)
        user_org = getattr(user, "org_id", None)
        if user_org and kb_org and kb_org != user_org:
            raise HTTPException(status_code=404, detail="KnowledgeBase not found")

        link = attach_kb_to_project(
            db, kb, project, created_by_id=user.id,
        )
        db.commit()
        return {
            "id": link.id,
            "project_id": project.id,
            "knowledge_base_id": kb.id,
            "role": link.role,
            "is_home": kb.project_id == project.id,
        }

    @router.delete("/projects/{project_id}/knowledge-bases/{kb_id}")
    def detach_kb_from_project_endpoint(
        app_id: str,
        project_id: str,
        kb_id: str,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        """Remove a KB's membership in a project. If the detached project was
        the KB's home, the home pointer moves to the first remaining linked
        project (or becomes None)."""
        from app.services.project_kb_membership import detach_kb_from_project

        project = db.query(Project).filter(
            Project.id == project_id,
            Project.is_deleted == False,
        ).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        ).first()
        if not kb:
            raise HTTPException(status_code=404, detail="KnowledgeBase not found")

        kb_org = getattr(kb, "org_id", None)
        user_org = getattr(user, "org_id", None)
        if user_org and kb_org and kb_org != user_org:
            raise HTTPException(status_code=404, detail="KnowledgeBase not found")

        removed = detach_kb_from_project(db, kb, project)
        db.commit()
        return {"detached": removed, "home_project_id": kb.project_id}

    return router
