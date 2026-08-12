"""Project Catalog API — Data Map backend.

Project-scoped REST endpoints for the semantic catalog, human curation
overlays, the entity graph, and the Unified Resource Registry:

    GET /projects/{project_id}/catalog/tables      — catalog view + overlay status
    PUT /projects/{project_id}/catalog/overlay     — upsert overlay (editors only)
    GET /projects/{project_id}/catalog/entities    — entity list + links
    GET /projects/{project_id}/registry/resources  — registry list (visibility-tiered)

Access follows the existing project-sharing pattern: project owner,
ResourceShare grantees (read), and org admins. Overlay writes require
owner/admin (shares are view/use-only).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required

logger = logging.getLogger(__name__)


# ── access helpers ─────────────────────────────────────────────────────────

def _load_project(db: Session, project_id: str):
    from app.models.project import Project

    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.is_deleted == False)  # noqa: E712
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _check_access(db: Session, project: Any, user: Any) -> tuple[bool, bool]:
    """Return (has_access, can_edit) following the ResourceShare pattern."""
    from app.models.resource_share import ResourceShare

    is_admin = getattr(user, "role", "user") == "admin"
    is_owner = project.created_by_id == user.id
    if is_admin or is_owner:
        return True, True
    share = (
        db.query(ResourceShare)
        .filter(
            ResourceShare.resource_id == project.id,
            ResourceShare.resource_type == "project",
            ResourceShare.shared_with_user_id == user.id,
            ResourceShare.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    return (share is not None), False


def _bound_kbs(db: Session, project: Any) -> list[Any]:
    """KBs bound to the project (exact FK or legacy project-name match)."""
    from sqlalchemy import or_

    from app.models.knowledge_base import KnowledgeBase

    return (
        db.query(KnowledgeBase)
        .filter(
            or_(
                KnowledgeBase.project_id == project.id,
                KnowledgeBase.project == project.name,
            ),
            KnowledgeBase.is_deleted == False,  # noqa: E712
        )
        .all()
    )


# ── router ─────────────────────────────────────────────────────────────────

def register_project_catalog_router() -> APIRouter:
    router = APIRouter(prefix="/api/apps/{app_id}", tags=["project-catalog"])

    @router.get("/projects/{project_id}/knowledge-map")
    def get_project_knowledge_map(
        app_id: str,
        project_id: str,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        """Business-facing, resource-general knowledge map summary.

        This endpoint aggregates project resources (visibility-aware), entities,
        entity links, and catalog table metadata into one payload the frontend can
        render as a user-facing Knowledge Map.
        """
        from app.models.knowledge_catalog import KBTableMeta, ProjectEntity, ProjectEntityLink
        from app.services.knowledge_graph.registry_indexer import list_project_resources

        project = _load_project(db, project_id)
        has_access, can_edit = _check_access(db, project, user)
        if not has_access:
            raise HTTPException(status_code=403, detail="You do not have access to this project")

        is_admin = getattr(user, "role", "user") == "admin"
        resources = list_project_resources(
            db,
            project_id,
            viewer_user_id=user.id,
            is_admin=is_admin,
        )

        resources_by_type: dict[str, list[dict[str, Any]]] = {}
        knowledge_areas: set[str] = set()
        last_indexed_at = None
        for r in resources:
            row = {
                "id": r.id,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "name": r.name,
                "summary": r.summary,
                "entities": r.entities or [],
                "visibility": r.visibility,
                "status": r.status,
                "last_indexed_at": (
                    r.last_indexed_at.isoformat() if r.last_indexed_at else None
                ),
            }
            resources_by_type.setdefault(r.resource_type, []).append(row)
            for token in (r.entities or []):
                if isinstance(token, str) and token.strip():
                    knowledge_areas.add(token.strip())
            if r.last_indexed_at and (last_indexed_at is None or r.last_indexed_at > last_indexed_at):
                last_indexed_at = r.last_indexed_at

        entities = (
            db.query(ProjectEntity)
            .filter(
                ProjectEntity.project_id == project_id,
                ProjectEntity.is_deleted == False,  # noqa: E712
            )
            .order_by(ProjectEntity.entity_type, ProjectEntity.name)
            .all()
        )
        links = (
            db.query(ProjectEntityLink)
            .filter(ProjectEntityLink.entity_id.in_([e.id for e in entities]))
            .all()
            if entities
            else []
        )
        links_by_entity: dict[str, list[dict[str, Any]]] = {}
        for l in links:
            links_by_entity.setdefault(l.entity_id, []).append(
                {
                    "target_type": l.target_type,
                    "target_id": l.target_id,
                    "confidence": l.confidence,
                    "source": l.source,
                }
            )

        entities_by_type: dict[str, list[dict[str, Any]]] = {}
        for e in entities:
            knowledge_areas.add(e.entity_type)
            row = {
                "id": e.id,
                "name": e.name,
                "aliases": e.aliases or [],
                "entity_type": e.entity_type,
                "description": e.description,
                "source": e.source,
                "links": links_by_entity.get(e.id, []),
            }
            entities_by_type.setdefault(e.entity_type, []).append(row)

        kbs = _bound_kbs(db, project)
        kb_map = {kb.id: kb for kb in kbs}
        tables: list[dict[str, Any]] = []
        if kb_map:
            metas = (
                db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id.in_(list(kb_map)))
                .order_by(KBTableMeta.kb_id, KBTableMeta.table_name)
                .all()
            )
            for m in metas:
                tables.append(
                    {
                        "kb_id": m.kb_id,
                        "kb_name": kb_map[m.kb_id].name,
                        "table_name": m.table_name,
                        "table_type": m.table_type,
                        "row_count": m.row_count,
                        "description_zh": m.description_zh,
                        "description_en": m.description_en,
                        "indexed_at": m.indexed_at.isoformat() if m.indexed_at else None,
                    }
                )

        needs_review = {
            "resources_in_error": sum(1 for r in resources if r.status == "error"),
            "resources_indexing": sum(1 for r in resources if r.status in ("pending", "indexing")),
            "entities_without_links": sum(1 for e in entities if not links_by_entity.get(e.id)),
            "tables_without_description": sum(
                1 for t in tables if not (t.get("description_zh") or t.get("description_en"))
            ),
        }

        return {
            "can_edit": can_edit,
            "summary": {
                "resource_count": len(resources),
                "entity_count": len(entities),
                "table_count": len(tables),
                "knowledge_area_count": len(knowledge_areas),
                "last_indexed_at": last_indexed_at.isoformat() if last_indexed_at else None,
            },
            "knowledge_areas": sorted(knowledge_areas),
            "entities_by_type": entities_by_type,
            "resources_by_type": resources_by_type,
            "tables": tables,
            "needs_review": needs_review,
        }

    @router.get("/projects/{project_id}/catalog/tables")
    def list_catalog_tables(
        app_id: str,
        project_id: str,
        search: str | None = Query(None, description="Filter by name/description"),
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        from app.models.knowledge_catalog import KBTableMeta, ProjectCatalogOverlay

        project = _load_project(db, project_id)
        has_access, can_edit = _check_access(db, project, user)
        if not has_access:
            raise HTTPException(status_code=403, detail="You do not have access to this project")

        kbs = _bound_kbs(db, project)
        kb_map = {kb.id: kb for kb in kbs}
        if not kb_map:
            return {"tables": [], "can_edit": can_edit, "kbs": []}

        metas = (
            db.query(KBTableMeta)
            .filter(KBTableMeta.kb_id.in_(list(kb_map)))
            .order_by(KBTableMeta.kb_id, KBTableMeta.table_name)
            .all()
        )
        overlays = (
            db.query(ProjectCatalogOverlay)
            .filter(
                ProjectCatalogOverlay.project_id == project_id,
                ProjectCatalogOverlay.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        overlay_map = {(o.kb_id, o.table_name): o for o in overlays}

        q = (search or "").strip().lower()
        tables = []
        for m in metas:
            ov = overlay_map.get((m.kb_id, m.table_name))
            row = {
                "kb_id": m.kb_id,
                "kb_name": kb_map[m.kb_id].name,
                "table_name": m.table_name,
                "table_type": m.table_type,
                "row_count": m.row_count,
                "description_zh": m.description_zh,
                "description_en": m.description_en,
                "indexed_at": m.indexed_at.isoformat() if m.indexed_at else None,
                "overlay": (
                    {
                        "alias": ov.alias,
                        "description": ov.description,
                        "metric_definition": ov.metric_definition,
                    }
                    if ov
                    else None
                ),
            }
            if q:
                hay = " ".join(
                    str(x or "")
                    for x in (
                        m.table_name, m.description_zh, m.description_en,
                        ov.alias if ov else "", ov.description if ov else "",
                    )
                ).lower()
                if q not in hay:
                    continue
            tables.append(row)

        return {
            "tables": tables,
            "can_edit": can_edit,
            "kbs": [
                {
                    "id": kb.id,
                    "name": kb.name,
                    "db_type": kb.db_type,
                    "catalog_status": getattr(kb, "catalog_status", None),
                    "catalog_last_indexed_at": (
                        kb.catalog_last_indexed_at.isoformat()
                        if getattr(kb, "catalog_last_indexed_at", None)
                        else None
                    ),
                }
                for kb in kbs
            ],
        }

    @router.put("/projects/{project_id}/catalog/overlay")
    def put_catalog_overlay(
        app_id: str,
        project_id: str,
        payload: dict,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        from app.models.knowledge_catalog import ProjectCatalogOverlay

        project = _load_project(db, project_id)
        has_access, can_edit = _check_access(db, project, user)
        if not has_access:
            raise HTTPException(status_code=403, detail="You do not have access to this project")
        if not can_edit:
            raise HTTPException(status_code=403, detail="Only the project owner or an admin can edit overlays")

        kb_id = payload.get("kb_id")
        table_name = payload.get("table_name")
        scope = payload.get("scope") or ("table" if table_name else "kb")
        if not kb_id:
            raise HTTPException(status_code=400, detail="kb_id is required")

        row = (
            db.query(ProjectCatalogOverlay)
            .filter(
                ProjectCatalogOverlay.project_id == project_id,
                ProjectCatalogOverlay.kb_id == kb_id,
                ProjectCatalogOverlay.table_name == table_name,
                ProjectCatalogOverlay.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if row is None:
            row = ProjectCatalogOverlay(
                project_id=project_id,
                kb_id=kb_id,
                table_name=table_name,
                scope=scope,
                created_by_id=user.id,
            )
            db.add(row)
        row.alias = payload.get("alias", row.alias)
        row.description = payload.get("description", row.description)
        row.metric_definition = payload.get("metric_definition", row.metric_definition)
        row.scope = scope
        db.commit()
        return {"success": True, "id": row.id}

    @router.get("/projects/{project_id}/catalog/entities")
    def list_catalog_entities(
        app_id: str,
        project_id: str,
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        from app.models.knowledge_catalog import ProjectEntity, ProjectEntityLink

        project = _load_project(db, project_id)
        has_access, _ = _check_access(db, project, user)
        if not has_access:
            raise HTTPException(status_code=403, detail="You do not have access to this project")

        entities = (
            db.query(ProjectEntity)
            .filter(
                ProjectEntity.project_id == project_id,
                ProjectEntity.is_deleted == False,  # noqa: E712
            )
            .order_by(ProjectEntity.entity_type, ProjectEntity.name)
            .all()
        )
        links = (
            db.query(ProjectEntityLink)
            .filter(ProjectEntityLink.entity_id.in_([e.id for e in entities]))
            .all()
        ) if entities else []
        links_by_entity: dict[str, list] = {}
        for l in links:
            links_by_entity.setdefault(l.entity_id, []).append(
                {
                    "target_type": l.target_type,
                    "target_id": l.target_id,
                    "confidence": l.confidence,
                    "source": l.source,
                }
            )
        return {
            "entities": [
                {
                    "id": e.id,
                    "name": e.name,
                    "aliases": e.aliases or [],
                    "entity_type": e.entity_type,
                    "description": e.description,
                    "source": e.source,
                    "links": links_by_entity.get(e.id, []),
                }
                for e in entities
            ]
        }

    @router.get("/projects/{project_id}/registry/resources")
    def list_registry_resources(
        app_id: str,
        project_id: str,
        resource_type: str | None = Query(None),
        db: Session = Depends(get_db),
        user=Depends(get_current_user_required),
    ):
        from app.services.knowledge_graph.registry_indexer import list_project_resources

        project = _load_project(db, project_id)
        has_access, can_edit = _check_access(db, project, user)
        if not has_access:
            raise HTTPException(status_code=403, detail="You do not have access to this project")

        is_admin = getattr(user, "role", "user") == "admin"
        rows = list_project_resources(
            db,
            project_id,
            viewer_user_id=user.id,
            is_admin=is_admin,
            resource_type=resource_type,
        )
        return {
            "resources": [
                {
                    "id": r.id,
                    "resource_type": r.resource_type,
                    "resource_id": r.resource_id,
                    "name": r.name,
                    "summary": r.summary,
                    "entities": r.entities or [],
                    "visibility": r.visibility,
                    "status": r.status,
                    "last_indexed_at": (
                        r.last_indexed_at.isoformat() if r.last_indexed_at else None
                    ),
                }
                for r in rows
            ],
            "can_edit": can_edit,
        }

    return router
