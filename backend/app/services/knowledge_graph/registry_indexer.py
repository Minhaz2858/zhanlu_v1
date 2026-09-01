"""Registry Indexer — idempotent upserts into the Unified Resource Registry.

This is a *thin umbrella*: it does NOT re-ingest anything. Existing
lifecycle events (KB catalog indexed, document ingested, memory written,
report generated) call these upserts so the project gains a single
visibility-tiered view over its resources.

Design notes:
- Sync-only; safe to call via ``asyncio.to_thread`` from triggers.
- Idempotent: upsert keyed by (project_id, resource_type, resource_id).
- Never raises into callers — registry writes are best-effort side data;
  failures are logged and swallowed by the caller-facing wrappers.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.resource_registry import ResourceRegistry

logger = logging.getLogger(__name__)

VALID_VISIBILITIES = {"project", "user_private", "org"}
VALID_STATUSES = {"pending", "indexing", "ready", "error"}


# ── upsert primitives ──────────────────────────────────────────────────────

def upsert_resource(
    db: Session,
    *,
    project_id: str,
    resource_type: str,
    resource_id: str | None,
    name: str,
    summary: str = "",
    entities: list[str] | None = None,
    owner_user_id: str | None = None,
    visibility: str = "project",
    status: str = "ready",
    org_id: str | None = None,
    app_id: str | None = None,
) -> ResourceRegistry:
    """Insert or update the registry row for one resource (idempotent)."""
    if visibility not in VALID_VISIBILITIES:
        visibility = "project"
    if status not in VALID_STATUSES:
        status = "ready"

    q = db.query(ResourceRegistry).filter(
        ResourceRegistry.project_id == project_id,
        ResourceRegistry.resource_type == resource_type,
        ResourceRegistry.resource_id == resource_id,
    )
    row = q.first()
    now = datetime.utcnow()
    if row is None:
        row = ResourceRegistry(
            project_id=project_id,
            resource_type=resource_type,
            resource_id=resource_id,
            name=name,
            summary=summary,
            entities=entities or [],
            owner_user_id=owner_user_id,
            visibility=visibility,
            status=status,
            last_indexed_at=now,
        )
        if org_id:
            row.org_id = org_id
        if app_id:
            row.app_id = app_id
        db.add(row)
    else:
        row.name = name
        row.summary = summary
        if entities is not None:
            row.entities = entities
        if owner_user_id is not None:
            row.owner_user_id = owner_user_id
        row.visibility = visibility
        row.status = status
        row.last_indexed_at = now
    db.flush()
    return row


def mark_status(
    db: Session,
    project_id: str,
    resource_type: str,
    resource_id: str | None,
    status: str,
) -> None:
    """Transition the status of an existing registry row (no-op if absent)."""
    if status not in VALID_STATUSES:
        return
    row = (
        db.query(ResourceRegistry)
        .filter(
            ResourceRegistry.project_id == project_id,
            ResourceRegistry.resource_type == resource_type,
            ResourceRegistry.resource_id == resource_id,
        )
        .first()
    )
    if row is not None:
        row.status = status
        db.flush()


# ── resource-specific indexers ─────────────────────────────────────────────

def index_knowledge_base(
    db: Session,
    kb: Any,
    *,
    project_id: str,
    table_count: int | None = None,
    visibility: str = "project",
) -> ResourceRegistry:
    """Register a connected database KB as a ``database`` resource."""
    db_type = getattr(kb, "db_type", "") or "database"
    if table_count:
        summary = f"{db_type} database with {table_count} cataloged tables."
    else:
        summary = f"{db_type} database."
    return upsert_resource(
        db,
        project_id=project_id,
        resource_type="database",
        resource_id=getattr(kb, "id", None),
        name=getattr(kb, "name", "") or "database",
        summary=summary,
        status="ready",
        visibility=visibility,
        org_id=getattr(kb, "org_id", None),
        app_id=getattr(kb, "app_id", None),
    )


def index_document(
    db: Session,
    *,
    project_id: str,
    document_id: str,
    name: str,
    summary: str = "",
    owner_user_id: str | None = None,
    visibility: str = "project",
) -> ResourceRegistry:
    """Register an uploaded file as a ``file`` resource (summary-level only)."""
    return upsert_resource(
        db,
        project_id=project_id,
        resource_type="file",
        resource_id=document_id,
        name=name,
        summary=summary,
        owner_user_id=owner_user_id,
        visibility=visibility,
        status="ready",
    )


def index_memory_entry(
    db: Session,
    *,
    project_id: str,
    memory_id: str,
    summary: str,
    entities: list[str] | None = None,
    owner_user_id: str | None = None,
    visibility: str = "project",
) -> ResourceRegistry:
    """Register a project-memory summary as a ``memory`` resource."""
    return upsert_resource(
        db,
        project_id=project_id,
        resource_type="memory",
        resource_id=memory_id,
        name=(summary or "memory")[:80],
        summary=summary,
        entities=entities,
        owner_user_id=owner_user_id,
        visibility=visibility,
        status="ready",
    )


def index_report(
    db: Session,
    *,
    project_id: str,
    report_id: str,
    name: str,
    summary: str = "",
    owner_user_id: str | None = None,
    visibility: str = "project",
) -> ResourceRegistry:
    """Register a generated report as a ``report`` resource."""
    return upsert_resource(
        db,
        project_id=project_id,
        resource_type="report",
        resource_id=report_id,
        name=name or "report",
        summary=summary,
        owner_user_id=owner_user_id,
        visibility=visibility,
        status="ready",
    )


def index_conversation(
    db: Session,
    *,
    project_id: str,
    conversation_id: str,
    title: str,
    summary: str = "",
    owner_user_id: str | None = None,
    visibility: str = "project",
) -> ResourceRegistry:
    """Register a project conversation summary as a ``conversation`` resource."""
    safe_title = (title or "conversation").strip() or "conversation"
    return upsert_resource(
        db,
        project_id=project_id,
        resource_type="conversation",
        resource_id=conversation_id,
        name=safe_title,
        summary=summary,
        owner_user_id=owner_user_id,
        visibility=visibility,
        status="ready",
    )


def index_decision(
    db: Session,
    *,
    project_id: str,
    decision_id: str,
    name: str,
    summary: str = "",
    owner_user_id: str | None = None,
    visibility: str = "project",
) -> ResourceRegistry:
    """Register a decision artifact as a ``decision`` resource."""
    return upsert_resource(
        db,
        project_id=project_id,
        resource_type="decision",
        resource_id=decision_id,
        name=name or "decision",
        summary=summary,
        owner_user_id=owner_user_id,
        visibility=visibility,
        status="ready",
    )


def index_automation(
    db: Session,
    *,
    project_id: str,
    automation_id: str,
    name: str,
    summary: str = "",
    owner_user_id: str | None = None,
    visibility: str = "project",
) -> ResourceRegistry:
    """Register a project automation task as an ``automation`` resource."""
    return upsert_resource(
        db,
        project_id=project_id,
        resource_type="automation",
        resource_id=automation_id,
        name=name or "automation",
        summary=summary,
        owner_user_id=owner_user_id,
        visibility=visibility,
        status="ready",
    )


# ── reads (visibility-enforced) ────────────────────────────────────────────

def list_project_resources(
    db: Session,
    project_id: str,
    *,
    viewer_user_id: str | None = None,
    is_admin: bool = False,
    resource_type: str | None = None,
) -> list[ResourceRegistry]:
    """List registry rows for a project, enforcing visibility tiers.

    - ``project`` / ``org`` rows: visible to every project member.
    - ``user_private`` rows: visible only to their owner (and admins).
    """
    q = db.query(ResourceRegistry).filter(
        ResourceRegistry.project_id == project_id,
        ResourceRegistry.is_deleted == False,  # noqa: E712
    )
    if resource_type:
        q = q.filter(ResourceRegistry.resource_type == resource_type)
    rows = q.order_by(ResourceRegistry.resource_type, ResourceRegistry.name).all()
    if is_admin:
        return rows
    return [
        r
        for r in rows
        if r.visibility in ("project", "org")
        or (r.visibility == "user_private" and r.owner_user_id == viewer_user_id)
    ]


__all__ = [
    "upsert_resource",
    "mark_status",
    "index_knowledge_base",
    "index_document",
    "index_memory_entry",
    "index_report",
    "index_conversation",
    "index_decision",
    "index_automation",
    "list_project_resources",
]
