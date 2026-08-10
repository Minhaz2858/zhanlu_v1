"""Custom endpoints for KnowledgeBase — connectivity test, document reindex, status,
discovery scan.

The generic entity router still owns CRUD; this router adds KB-specific
actions that don't fit the generic CRUD shape: a pre-save connectivity
test, document reindex, indexing status, and manual discovery re-scan.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models.knowledge_base import KnowledgeBase
from app.services.db.connector_factory import (
    DriverUnavailable,
    get_connector,
)
from app.services.document_ingestion import service as ingestion_service
from app.services.forecasting.discovery import discover as run_discovery
from app.services.universal_analytics.auto_discover import _write_targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps/{app_id}/knowledge_bases", tags=["KnowledgeBase"])


class TestConnectionRequest(BaseModel):
    """Form fields the user typed in the Connectors wizard. No KB id yet."""

    db_type: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = None
    api_url: str | None = None


class _KbShim:
    """Minimal stand-in for a KnowledgeBase row.

    The connector factory only reads these attributes, so a tiny shim is
    enough to drive the existing ``test_connection()`` path before the
    KB row actually exists in the database.
    """

    def __init__(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _check_database_exists(engine: object, db_type: str, name: str) -> dict:
    """Verify the named database actually exists on the server.

    Each dialect exposes the catalog differently; Oracle and SQLite are
    skipped (Oracle is a single-instance catalog, SQLite is a file path).
    The caller passes an *open* SQLAlchemy ``Engine`` so we can run a
    catalog query without rebuilding a connection pool. Returns
    ``{"ok": bool, "info": str}``.
    """

    dt = (db_type or "").lower()
    if dt in ("mysql", "mariadb"):
        sql = "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :n"
    elif dt in ("postgres", "postgresql"):
        sql = "SELECT 1 FROM pg_database WHERE datname = :n"
    elif dt in ("mssql", "sqlserver"):
        sql = "SELECT 1 FROM sys.databases WHERE name = :n"
    else:
        return {"ok": True, "info": f"database check skipped for {dt or 'unknown'}"}
    try:
        with engine.connect() as c:  # type: ignore[attr-defined]
            row = c.execute(text(sql), {"n": name}).first()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "info": f"Database check failed: {e}"}
    if row is None:
        return {"ok": False, "info": f"Database '{name}' does not exist on the server."}
    return {"ok": True, "info": f"database '{name}' exists"}


@router.post("/test_connection")
async def test_connection(
    app_id: str,  # noqa: ARG001 — kept for path parity with the rest of the router
    payload: TestConnectionRequest,
) -> dict:
    """Test a database connection without saving a KnowledgeBase row.

    Runs the existing connector's ``test_connection()`` path (TCP + auth
    + ``SELECT VERSION()``) and, if a ``database_name`` is provided,
    also verifies that database actually exists on the server. This
    catches the common mistake of typing a wrong db_name (e.g. ``prod5``
    instead of ``prod``) before the user ever saves a broken row.

    The endpoint is intentionally non-mutating and does not touch the
    application database; the user can still save the KB regardless of
    the result.
    """
    if not payload.db_type or payload.db_type == "api":
        return {"ok": False, "info": "API endpoints do not support a connectivity test."}
    if not payload.host:
        return {"ok": False, "info": "Host is required."}
    if not payload.username:
        return {"ok": False, "info": "Username is required."}

    shim = _KbShim(
        id="test-shim",
        db_type=payload.db_type,
        host=payload.host,
        port=payload.port or 0,
        database_name=payload.database_name,
        username=payload.username,
        password=payload.password or "",
    )

    def _do_test() -> dict:
        try:
            conn = get_connector(shim)
        except DriverUnavailable as e:
            return {"ok": False, "info": str(e)}
        except ValueError as e:
            return {"ok": False, "info": str(e)}
        # The connector's context manager creates the SQLAlchemy engine
        # on __enter__ and disposes it on __exit__. We keep it open for
        # both the version probe AND the database-existence probe so we
        # don't have to build a second engine just for the second query.
        try:
            with conn:
                try:
                    with conn._engine.connect() as c:  # type: ignore[attr-defined]
                        v = c.execute(text("SELECT VERSION()")).scalar()
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "info": str(e)}
                base_info = f"{_humanize_db_type(payload.db_type)} {v}"
                if payload.database_name:
                    dbi = _check_database_exists(
                        conn._engine,  # type: ignore[attr-defined]
                        payload.db_type,
                        payload.database_name,
                    )
                    if not dbi.get("ok"):
                        return dbi
                    base_info = f"{base_info} · {dbi['info']}"
                return {"ok": True, "info": base_info}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "info": str(e)}

    return await asyncio.to_thread(_do_test)


def _humanize_db_type(db_type: str) -> str:
    """Human label for the version line so the UI can show 'MySQL 8.4.5'."""
    return {
        "mysql": "MySQL",
        "mariadb": "MariaDB",
        "postgres": "PostgreSQL",
        "postgresql": "PostgreSQL",
        "mssql": "MSSQL",
        "sqlserver": "SQL Server",
        "oracle": "Oracle",
        "sqlite": "SQLite",
    }.get((db_type or "").lower(), db_type or "DB")


@router.post("/{kb_id}/reindex")
async def reindex_kb(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Re-extract, re-chunk, re-embed a file-kind KB. Returns the new status."""
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    if kb.source_kind != "file":
        raise HTTPException(
            status_code=400,
            detail=f"Reindex is only for source_kind='file' (got {kb.source_kind!r})",
        )
    # Run ingestion in a thread so we don't block the event loop.
    ok = await asyncio.to_thread(ingestion_service.ingest_kb, db, kb_id)
    status = ingestion_service.get_status(db, kb_id)

    # Also trigger catalog reindex if this is a db-type KB
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if kb:
            from app.services.knowledge_graph.catalog_triggers import maybe_reindex_catalog
            await maybe_reindex_catalog(kb)
    except Exception:
        logger.warning("catalog: reindex trigger failed for kb=%s", kb_id, exc_info=True)

    return {"success": ok, **status}


@router.post("/{kb_id}/catalog/reindex")
async def reindex_catalog(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Manually trigger catalog (re)index for a database KnowledgeBase.

    Spawns the catalog indexer as a background task and returns 202 immediately.
    Only meaningful when SEMANTIC_CATALOG_ENABLED=True and db_type in (mysql, postgres).
    """
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    if (kb.db_type or "").lower() not in ("mysql", "postgres", "postgresql"):
        raise HTTPException(
            status_code=400,
            detail=f"Catalog index is only for db_type='mysql'/'postgres' (got {kb.db_type!r})",
        )

    from app.services.knowledge_graph.catalog_triggers import maybe_reindex_catalog
    await maybe_reindex_catalog(kb)
    return {
        "status": "accepted",
        "catalog_status": kb.catalog_status,
        "kb_id": kb_id,
    }


@router.get("/{kb_id}/catalog/status")
async def catalog_status(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Return the catalog indexing status for a database KB."""
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    return {
        "kb_id": kb_id,
        "catalog_status": kb.catalog_status,
    }


@router.get("/{kb_id}/status")
async def kb_status(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Return the indexing status of a file-kind KB."""
    status = ingestion_service.get_status(db, kb_id)
    if not status.get("found"):
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    return status


@router.post("/{kb_id}/discover")
async def discover_kb(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Manual re-scan: discover forecastable time series in a database KB.

    Runs the discovery scanner against all tables, writes new
    ForecastTarget rows, and returns the candidates found.  Always
    available (not flag-gated) — this is an explicit user action.

    Only works for source_kind='db' KBs; file-kind KBs return 400.
    """
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    if getattr(kb, "source_kind", "db") not in ("db", "database"):
        raise HTTPException(
            status_code=400,
            detail=f"Discovery is only for database-kind KBs (got source_kind={kb.source_kind!r})",
        )

    # Run the scanner (synchronous — may take 10-60 seconds on large DBs).
    candidates = await asyncio.to_thread(run_discovery, db, kb_id)

    # Write ForecastTarget rows, skipping duplicates.
    org_id = getattr(kb, "org_id", "default")
    written = _write_targets(db, kb_id, org_id, candidates)
    db.commit()

    return {
        "success": True,
        "kb_id": kb_id,
        "tables_scanned": len(set(c.get("table", "") for c in candidates)),
        "candidates_found": len(candidates),
        "targets_written": written,
        "candidates": candidates[:20],  # limit detail to first 20
    }
