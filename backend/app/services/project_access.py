"""Project-reference authorization (Phase 0A, 2026-11-01).

A project reference supplied by a client (body ``project_id`` /
``project_name`` / conversation metadata) must be AUTHORIZED before its
resources, memory or LLM configuration may influence an AI conversation.
Existence alone is not authorization (verified Phase-0 gap: any
authenticated user could point a conversation at any live project id and
inherit its KBs via the data-source runtime).

Authorization semantics (org-scoped, fail-closed):

- the project must belong to the caller's organization (cross-org is a
  404, never a distinguishable error);
- ORG ADMINISTRATOR (``user.role == "admin"`` within their own org) is
  authorized — mirrors ``require_org_admin`` semantics;
- the PROJECT CREATOR (``Project.created_by_id``) is authorized;
- an explicit ``ResourceShare`` grant on the project with
  ``access_level='use'`` ("view + run agents", per the model docstring)
  is authorized; the reserved ``'view'`` level does NOT authorize
  runtime execution;
- platform-shipped system projects (``Project.is_system``) are runnable
  by any user of the same organization (marketplace/system-agent flows).

Missing or soft-deleted projects are NOT errors — callers drop the
reference gracefully (stale browser sessionStorage must not hard-fail).
An EXISTING project the user is not authorized for raises HTTP 404 so
project existence is never leaked and the turn never runs with another
owner's resource scope.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def project_use_authorized(db: Session, user, project) -> bool:
    """Pure authorization check for running AI against ``project``."""
    if project is None:
        return False
    # 1) Organization ownership — the hard wall (RLS ring 1).
    if getattr(project, "org_id", None) != getattr(user, "org_id", None):
        return False
    # 2) Platform-shipped system projects: runnable in-org.
    if getattr(project, "is_system", False):
        return True
    # 3) Org administrator (their own org only).
    if getattr(user, "role", None) == "admin":
        return True
    # 4) Project creator / owner.
    creator = getattr(project, "created_by_id", None)
    if creator and str(creator) == str(getattr(user, "id", None)):
        return True
    # 5) Explicit 'use' share grant (view-only grants do NOT authorize runs).
    try:
        from app.models.resource_share import ResourceShare

        grant = (
            db.query(ResourceShare)
            .filter(
                ResourceShare.resource_type == "project",
                ResourceShare.resource_id == project.id,
                ResourceShare.shared_with_user_id == str(user.id),
                ResourceShare.access_level == "use",
                ResourceShare.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        return grant is not None
    except Exception:  # noqa: BLE001 — authorization must fail CLOSED
        logger.warning(
            "project_access: share lookup failed for project %s — denying",
            getattr(project, "id", "?"),
        )
        return False


def load_live_project(db: Session, project_id: str):
    """The project row if it exists and is not soft-deleted, else None."""
    from app.models.project import Project

    return (
        db.query(Project)
        .filter(Project.id == project_id, Project.is_deleted == False)  # noqa: E712
        .first()
    )


def authorize_project_reference(db: Session, user, project_id: str):
    """Resolve a client-supplied project reference under authorization.

    Returns the live ``Project`` row when authorized. Raises HTTP 404 for
    an existing project the user may not use (no existence leak). Returns
    None for missing / soft-deleted ids (callers degrade gracefully).
    """
    project = load_live_project(db, project_id) if project_id else None
    if project is None:
        return None
    if not project_use_authorized(db, user, project):
        logger.info(
            "project_access: user %s denied project %s (org=%s role=%s) — 404",
            getattr(user, "id", "?"), project.id, project.org_id,
            getattr(user, "role", "?"),
        )
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def resolve_authorized_message_project(
    db: Session,
    *,
    user,
    conv,
    body_project_id=None,
    body_project_name=None,
) -> str | None:
    """Authorized version of ``resolve_message_project_id`` for the chat
    message entry points (v2 + v3 stream).

    Reuses the existing precedence (body id > body name > conv project),
    then authorizes the RESOLVED reference — so a spoofed id OR name is
    rejected identically. Unauthorized existing project → HTTP 404;
    missing/deleted → dropped (falls back the same way the legacy
    resolver did); authorized → project id.
    """
    from app.services.llm_router import resolve_message_project_id

    conv_project_id = getattr(conv, "project_id", None)
    candidate = resolve_message_project_id(
        db,
        conv_project_id=conv_project_id,
        body_project_id=body_project_id,
        body_project_name=body_project_name,
    )
    if not candidate:
        return None
    # A conversation's persisted project was authorized at creation; a
    # body-supplied candidate that equals it keeps that trust unless the
    # project has since become unauthorized (revoked share / role change)
    # — re-checking is cheap and fail-closed.
    project = authorize_project_reference(db, user, candidate)
    return project.id if project is not None else None
