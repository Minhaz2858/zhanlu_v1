"""Terminologies authoring API — the VERIFIED-tier glossary input (P1-3).

The SemanticStore consumes ``terminologies`` rows as VERIFIED business
glossary knowledge (feed #4 of the OCI semantic-store design), but until
now there was NO way for an analyst to author them — the tier stayed
thin. This router exposes the existing ``TerminologyService`` CRUD:

    GET    /api/datasources/{datasource_id}/terminologies         list (all)
    POST   /api/datasources/{datasource_id}/terminologies         upsert
    PATCH  /api/datasources/{datasource_id}/terminologies/{id}    update
    DELETE /api/datasources/{datasource_id}/terminologies/{id}    soft delete
    GET    /api/datasources/{datasource_id}/terminologies/search  semantic search

Authenticated endpoints, scoped to one datasource per call. Upserts are
keyed by (word, datasource_ids) exactly like the service's read path, so
an authored entry is immediately visible to retrieval and grounding.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required

logger = logging.getLogger(__name__)


def register_terminologies_router() -> APIRouter:
    router = APIRouter(tags=["terminologies"])

    def _svc(db: Session = Depends(get_db)):
        from app.services.terminology.service import TerminologyService

        return TerminologyService(db)

    def _entry_or_404(svc, entry_id: str):
        from app.models.terminology import Terminology

        row = (
            svc._db.query(Terminology)
            .filter(
                Terminology.id == entry_id,
                Terminology.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Terminology not found")
        return row

    def _serialize(row) -> dict:
        return {
            "id": row.id,
            "word": row.word,
            "description": row.description,
            "parent_id": row.parent_id,
            "datasource_ids": row.datasource_ids,
            "agent_id": row.agent_id,
            "enabled": row.enabled,
            "created_date": str(row.created_date) if row.created_date else None,
        }

    @router.get("/datasources/{datasource_id}/terminologies")
    def list_terminologies(
        datasource_id: str,
        include_disabled: bool = Query(default=True),
        svc=Depends(_svc),
        user=Depends(get_current_user_required),
    ):
        from app.models.terminology import Terminology

        rows = (
            svc._db.query(Terminology)
            .filter(Terminology.is_deleted == False)  # noqa: E712
            .order_by(Terminology.word.asc())
            .limit(500)
            .all()
        )
        rows = [r for r in rows if datasource_id in _datasource_ids_of(r)]
        if not include_disabled:
            rows = [r for r in rows if r.enabled]
        return [_serialize(r) for r in rows]

    @router.post("/datasources/{datasource_id}/terminologies")
    def upsert_terminology(
        datasource_id: str,
        body: dict,
        svc=Depends(_svc),
        user=Depends(get_current_user_required),
    ):
        word = str((body or {}).get("word") or "").strip()
        description = str((body or {}).get("description") or "").strip()
        if not word:
            raise HTTPException(status_code=400, detail="word is required")
        row = svc.upsert(
            word,
            description,
            datasource_ids=[datasource_id],
        )
        return _serialize(row)

    @router.patch("/datasources/{datasource_id}/terminologies/{entry_id}")
    def update_terminology(
        datasource_id: str,
        entry_id: str,
        body: dict,
        svc=Depends(_svc),
        user=Depends(get_current_user_required),
    ):
        row = _entry_or_404(svc, entry_id)
        if datasource_id not in _datasource_ids_of(row):
            raise HTTPException(
                status_code=404, detail="Terminology not found on this datasource"
            )
        if "description" in (body or {}):
            row.description = str(body.get("description") or "").strip()
        if "enabled" in (body or {}):
            row.enabled = bool(body.get("enabled"))
        svc._db.commit()
        return _serialize(row)

    @router.delete("/datasources/{datasource_id}/terminologies/{entry_id}")
    def delete_terminology(
        datasource_id: str,
        entry_id: str,
        svc=Depends(_svc),
        user=Depends(get_current_user_required),
    ):
        row = _entry_or_404(svc, entry_id)
        if datasource_id not in _datasource_ids_of(row):
            raise HTTPException(
                status_code=404, detail="Terminology not found on this datasource"
            )
        row.is_deleted = True
        svc._db.commit()
        return {"ok": True}

    @router.get("/datasources/{datasource_id}/terminologies/search")
    def search_terminologies(
        datasource_id: str,
        q: str = Query(..., min_length=1),
        top_k: int = Query(default=3, le=10),
        svc=Depends(_svc),
        user=Depends(get_current_user_required),
    ):
        hits = svc.search_by_word(q, datasource_id, top_k=top_k)
        return [
            {"word": w, "description": d, "score": round(s, 4)}
            for w, d, s in hits
        ]

    return router


def _datasource_ids_of(row) -> list[str]:
    import json as _json

    raw = getattr(row, "datasource_ids", None)
    if not raw:
        return []
    try:
        parsed = _json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []
