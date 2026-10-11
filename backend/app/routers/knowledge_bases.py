"""Custom endpoints for KnowledgeBase — connectivity test, document reindex, status,
discovery scan.

The generic entity router still owns CRUD; this router adds KB-specific
actions that don't fit the generic CRUD shape: a pre-save connectivity
test, document reindex, indexing status, and manual discovery re-scan.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.deps import get_current_user_required, get_db
from app.models.knowledge_base import KnowledgeBase
from app.routers.integrations import _upload_blocked_reason
from app.services.db.connector_factory import (
    DriverUnavailable,
    get_connector,
)
from app.services.document_ingestion import service as ingestion_service
from app.services.file_datasource import (
    ALLOWED_EXTS,
    import_spreadsheet_to_sqlite,
)
from app.services.forecasting.discovery import discover as run_discovery
from app.services.universal_analytics.auto_discover import _write_targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps/{app_id}/knowledge_bases", tags=["KnowledgeBase"], dependencies=[Depends(get_current_user_required)])


class TestConnectionRequest(BaseModel):
    """Form fields the user typed in the Connectors wizard. No KB id yet."""

    model_config = ConfigDict(populate_by_name=True)

    db_type: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    # ``schema`` collides with BaseModel.schema (deprecated method), so the
    # Python attribute is named ``schema_name`` while the wire alias stays
    # ``schema`` to keep the API contract and avoid a Pydantic shadow warning.
    schema_name: str | None = Field(default=None, alias="schema")
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
        schema=payload.schema_name or "public",
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


@router.post("/upload-query-file")
async def upload_query_file(
    app_id: str,
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form(""),
    project: str = Form("global"),
    user=Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Upload a CSV/XLSX and register it as a queryable SQLite datasource.

    The spreadsheet is parsed into a local SQLite file and registered as a
    KnowledgeBase row with ``db_type='sqlite'`` (``file_url`` = the sqlite
    path), so it shows up in the workflow Database Query node's picker
    (``listDatabases`` = rows with a truthy ``db_type``) and runs through the
    existing SQLiteConnector — SQL and natural-language mode both work with
    no runtime changes. Returns the created KB row (entities-router shape).
    """
    # ── Settings gate (file_upload_enabled) — same as Core/UploadFile ───
    blocked = _upload_blocked_reason(user, db)
    if blocked:
        raise HTTPException(status_code=403, detail=blocked)

    raw_name = file.filename or ""
    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext or '(none)'}' — upload a .csv, .xlsx or .xls spreadsheet",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        parsed = import_spreadsheet_to_sqlite(data, raw_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    kb = KnowledgeBase(
        name=(name.strip() or Path(raw_name).stem)[:255],
        description=(description.strip() or None),
        project=(project.strip() or "global"),
        source_kind="database",
        type="sqlite",
        db_type="sqlite",
        file_url=str(parsed["db_path"]),
        file_type=ext.lstrip("."),
        item_count=parsed["total_rows"],
        status="active",
        created_by_id=getattr(user, "id", None),
        # NOTE: app_id is NOT taken from the URL — the platform treats it as
        # cosmetics (entities router create ignores it; rows carry the model
        # default 'default-app') so lists scoped to the real app still see it.
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    logger.info(
        "upload-query-file: registered %s as sqlite datasource %s (%d rows, tables=%s)",
        kb.name, kb.id, kb.item_count, parsed["tables"],
    )
    return kb.to_dict()


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


@router.get("/capabilities")
async def capabilities(app_id: str):
    """DataSource capability manifest — the ONE place dialect support is declared.

    ``db_types``          every dialect the connector factory can connect to.
    ``catalog_db_types``  dialects the semantic catalog can index (a subset).
    ``catalog_enabled``   whether the catalog feature flag is on.

    The frontend calls this instead of carrying its own hardcoded
    ['mysql','postgres','postgresql'] list, so adding a dialect to
    connector_factory._REGISTRY lights it up end-to-end with no UI change.
    """
    from app.config import settings
    from app.services.db.connector_factory import supported_db_types
    from app.services.knowledge_graph.catalog_triggers import catalog_db_types
    return {
        "db_types": supported_db_types(),
        "catalog_db_types": catalog_db_types(),
        "catalog_enabled": bool(getattr(settings, "SEMANTIC_CATALOG_ENABLED", False)),
    }


@router.post("/{kb_id}/catalog/reindex")
async def reindex_catalog(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Manually trigger catalog (re)index for a database KnowledgeBase.

    Spawns the catalog indexer as a background task and returns 202 immediately.
    Meaningful for any dialect the connector factory supports — the gate is
    ``catalog_triggers.supports_catalog``, derived from the connector registry,
    NOT a hardcoded ('mysql','postgres') tuple (which used to 400 a MariaDB /
    SQL Server / Oracle / SQLite user's Refresh click).
    Also drops the in-process schema cache so describe_schema picks up the
    rebuilt catalog immediately.
    """
    from app.config import settings
    from app.services.knowledge_graph.catalog_triggers import supports_catalog
    try:
        from app.services.db.schema_service import invalidate_schema_cache
        invalidate_schema_cache(kb_id)
    except Exception:
        pass
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    if not settings.SEMANTIC_CATALOG_ENABLED:
        # Honest no-op: the UI would otherwise show "submitted" and the user
        # would wait for a catalog that never arrives.
        return {
            "status": "skipped",
            "reason": "catalog_disabled",
            "catalog_status": kb.catalog_status,
            "kb_id": kb_id,
        }
    if not supports_catalog(kb.db_type):
        from app.services.knowledge_graph.catalog_triggers import catalog_db_types
        supported = ", ".join(catalog_db_types()) or "none"
        raise HTTPException(
            status_code=400,
            detail=(
                f"Catalog index is not available for db_type={kb.db_type!r} "
                f"(catalogable dialects: {supported})"
            ),
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


@router.get("/{kb_id}/semantic-store")
async def get_semantic_store_view(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Return the unified SEMANTIC LAYER for a database KnowledgeBase.

    This is the OCI 'offline enrichment → semantic store' layer, made visible:
    relations (tables/views + business meaning + provenance tier), column
    semantics (semantic_type/unit/enum), join paths, business terms and metric
    definitions (glossary), workload signals (mined usage), mined frequent
    joins/filters, and business domains. Every item carries its provenance
    tier: VERIFIED (human-authored) / DISCOVERED (indexed) /
    LEARNED_FROM_WORKLOAD (mined) — the same tiers the online NL2SQL pipeline
    (planning → query-log retrieval → table retrieval → schema linking →
    SQL generation → rewriting → validation) consumes.
    """
    import dataclasses

    from app.config import settings
    from app.services.db.semantic_store import get_semantic_store

    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")

    enabled = bool(getattr(settings, "SEMANTIC_STORE_ENABLED", True))
    if not enabled:
        return {"kb_id": kb_id, "enabled": False, "stats": {}, "relations": [],
                "columns": [], "joins": [], "glossary": [], "workload": [],
                "mined": {"join_hints": [], "common_filters": []}, "domains": []}

    store = get_semantic_store(db, [kb_id])

    def _dc(obj) -> dict:
        d = dataclasses.asdict(obj)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        return d

    relations = [_dc(r) for r in store.relations()]
    table_names = [r["table_name"] for r in relations]

    # Attach catalog ids so the UI can edit meanings in place (the PATCH
    # catalog/tables/{id} endpoint keys on KBTableMeta.id).
    from app.models.knowledge_catalog import KBTableMeta as _KM

    _meta_ids = {
        t.table_name: str(t.id)
        for t in db.query(_KM).filter(_KM.kb_id == kb_id).all()
    }
    for r in relations:
        r["table_meta_id"] = _meta_ids.get(r["table_name"])

    # Column semantics: only the MEANINGFUL ones (described, typed, unitized,
    # enum-carrying or key columns) — the raw type dump is already in
    # catalog/tables; this view is about business meaning.
    columns: list[dict] = []
    _uninteresting = {"unknown", ""}
    for rel in relations:
        try:
            cols = store.columns(rel["table_name"])
        except Exception:
            continue
        for c in cols:
            cd = _dc(c)
            if (
                (cd.get("description") or "").strip()
                or cd.get("semantic_type") not in _uninteresting
                or cd.get("unit")
                or cd.get("enum_values")
                or cd.get("is_primary_key")
            ):
                columns.append(cd)
        if len(columns) > 2000:
            break

    glossary = [_dc(t) for t in store.glossary(None, limit=500)]
    workload = sorted(
        (_dc(w) for w in store.workload().values()),
        key=lambda w: -(w.get("uses") or 0),
    )
    # Joins can number in the thousands (every FK edge of every indexed
    # table) — ship the top 200 by confidence; stats.joins keeps the total.
    all_joins = sorted(store.joins(), key=lambda j: -float(j.confidence or 0))
    mined_joins = store.join_hints(k=10)
    mined_filters = store.common_filters(table_names, k=15)

    return {
        "kb_id": kb_id,
        "kb_name": kb.name,
        "enabled": True,
        "stats": store.stats(),
        "relations": relations,
        "columns": columns,
        "joins": [_dc(j) for j in all_joins[:200]],
        "joins_total": len(all_joins),
        "glossary": glossary,
        "guidelines": store.guidelines(),
        "workload": workload,
        "mined": {"join_hints": mined_joins, "common_filters": mined_filters},
        "domains": store.domains(),
    }


@router.get("/{kb_id}/metrics")
async def list_kb_metrics(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Metric definitions authored for this datasource — the "did = revenue vs
    margin" layer. MetricDefinition rows are datasource-scoped and consumed by
    SemanticStore.glossary as VERIFIED-tier vocabulary, so an authored metric
    immediately resolves ambiguous business words in NL2SQL."""
    from app.models.metric_definition import MetricDefinition

    rows = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.datasource_id == kb_id,
            MetricDefinition.is_deleted == False,  # noqa: E712
        )
        .order_by(MetricDefinition.name.asc())
        .all()
    )
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "display_name": r.display_name,
            "description": r.description,
            "base_table": r.base_table,
            "base_column": r.base_column,
            "aggregation": r.aggregation,
            "unit": r.unit,
            "synonyms": r.synonyms or [],
            "is_enabled": bool(r.is_enabled),
        }
        for r in rows
    ]


@router.post("/{kb_id}/metrics")
async def create_kb_metric(app_id: str, kb_id: str, body: dict, db: Session = Depends(get_db)):
    """Author a metric definition for this datasource (VERIFIED on save)."""
    import uuid as _uuid

    from app.models.metric_definition import MetricDefinition

    name = str((body or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    dup = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.datasource_id == kb_id,
            MetricDefinition.name == name,
            MetricDefinition.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if dup:
        raise HTTPException(status_code=409, detail=f"Metric '{name}' already exists")
    synonyms = body.get("synonyms") or []
    if isinstance(synonyms, str):
        synonyms = [s.strip() for s in synonyms.split(",") if s.strip()]
    row = MetricDefinition(
        id=str(_uuid.uuid4()),
        datasource_id=kb_id,
        name=name,
        display_name=str(body.get("display_name") or name),
        description=str(body.get("description") or "").strip() or None,
        base_table=str(body.get("base_table") or "").strip() or None,
        base_column=str(body.get("base_column") or "").strip() or None,
        aggregation=str(body.get("aggregation") or "").strip() or None,
        unit=str(body.get("unit") or "").strip() or None,
        synonyms=synonyms,
        is_enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": str(row.id), "name": row.name}


@router.patch("/{kb_id}/metrics/{metric_id}")
async def update_kb_metric(
    app_id: str, kb_id: str, metric_id: str, body: dict, db: Session = Depends(get_db)
):
    """Edit a metric definition (fix a wrong definition/unit/synonyms)."""
    from app.models.metric_definition import MetricDefinition

    row = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.id == metric_id,
            MetricDefinition.datasource_id == kb_id,
            MetricDefinition.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Metric not found")
    fields = {
        "description": str,
        "base_table": str,
        "base_column": str,
        "aggregation": str,
        "unit": str,
        "display_name": str,
    }
    for field, cast in fields.items():
        if field in (body or {}):
            val = body[field]
            setattr(row, field, (str(val).strip() or None) if val is not None else None)
    if "synonyms" in (body or {}):
        syn = body["synonyms"]
        row.synonyms = (
            [s.strip() for s in str(syn).split(",") if s.strip()]
            if isinstance(syn, str)
            else list(syn or [])
        )
    if "is_enabled" in (body or {}):
        row.is_enabled = bool(body["is_enabled"])
    db.commit()
    return {"id": str(row.id), "ok": True}


@router.delete("/{kb_id}/metrics/{metric_id}")
async def delete_kb_metric(
    app_id: str, kb_id: str, metric_id: str, db: Session = Depends(get_db)
):
    from app.models.metric_definition import MetricDefinition

    row = (
        db.query(MetricDefinition)
        .filter(
            MetricDefinition.id == metric_id,
            MetricDefinition.datasource_id == kb_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Metric not found")
    row.is_deleted = True
    db.commit()
    return {"ok": True}


@router.get("/{kb_id}/guidelines")
async def list_kb_guidelines(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Authored organizational rules (fiscal calendar, default filters,
    territory maps, exceptions, policies) for this datasource."""
    from app.models.semantic_guideline import SemanticGuideline

    rows = (
        db.query(SemanticGuideline)
        .filter(
            SemanticGuideline.datasource_id == kb_id,
            SemanticGuideline.is_deleted == False,  # noqa: E712
        )
        .order_by(SemanticGuideline.kind.asc(), SemanticGuideline.title.asc())
        .all()
    )
    return [
        {
            "id": str(r.id),
            "kind": r.kind,
            "title": r.title,
            "content": r.content,
            "is_enabled": bool(r.is_enabled),
        }
        for r in rows
    ]


@router.post("/{kb_id}/guidelines")
async def create_kb_guideline(app_id: str, kb_id: str, body: dict, db: Session = Depends(get_db)):
    """Author an organizational rule — becomes a BUSINESS RULES constraint
    injected into every NL2SQL generation for this datasource."""
    import uuid as _uuid

    from app.models.semantic_guideline import (
        GUIDELINE_KINDS,
        SemanticGuideline,
    )

    title = str((body or {}).get("title") or "").strip()
    content = str((body or {}).get("content") or "").strip()
    kind = str((body or {}).get("kind") or "other").strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="title and content are required")
    if kind not in GUIDELINE_KINDS:
        kind = "other"
    row = SemanticGuideline(
        id=str(_uuid.uuid4()),
        datasource_id=kb_id,
        kind=kind,
        title=title,
        content=content,
        is_enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": str(row.id), "kind": row.kind,
        "title": row.title, "content": row.content,
        "is_enabled": bool(row.is_enabled),
    }


@router.patch("/{kb_id}/guidelines/{guideline_id}")
async def update_kb_guideline(
    app_id: str, kb_id: str, guideline_id: str, body: dict, db: Session = Depends(get_db)
):
    from app.models.semantic_guideline import (
        GUIDELINE_KINDS,
        SemanticGuideline,
    )

    row = (
        db.query(SemanticGuideline)
        .filter(
            SemanticGuideline.id == guideline_id,
            SemanticGuideline.datasource_id == kb_id,
            SemanticGuideline.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Guideline not found")
    if "title" in (body or {}):
        row.title = str(body["title"]).strip() or row.title
    if "content" in (body or {}):
        row.content = str(body["content"]).strip() or row.content
    if "kind" in (body or {}) and body["kind"] in GUIDELINE_KINDS:
        row.kind = body["kind"]
    if "is_enabled" in (body or {}):
        row.is_enabled = bool(body["is_enabled"])
    db.commit()
    return {"id": str(row.id), "ok": True}


@router.delete("/{kb_id}/guidelines/{guideline_id}")
async def delete_kb_guideline(
    app_id: str, kb_id: str, guideline_id: str, db: Session = Depends(get_db)
):
    from app.models.semantic_guideline import SemanticGuideline

    row = (
        db.query(SemanticGuideline)
        .filter(
            SemanticGuideline.id == guideline_id,
            SemanticGuideline.datasource_id == kb_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Guideline not found")
    row.is_deleted = True
    db.commit()
    return {"ok": True}


@router.get("/{kb_id}/catalog/tables")
async def list_catalog_tables(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Return the catalog tables discovered for a database KnowledgeBase.

    Returns kb metadata (id, name, catalog_status, item_count) plus a list of
    tables with name, type, row_count, column_count, descriptions, column
    names (comma-joined for search), and indexed_at timestamp.
    """
    from app.models.knowledge_catalog import KBTableMeta, KBColumnMeta

    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")

    tables = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id)
        .order_by(KBTableMeta.schema_name, KBTableMeta.table_name)
        .all()
    )

    # Single aggregate query for column counts, grouped by table_meta_id.
    # Column names are collected in-Python for portability across PG / SQLite
    # (PG lacks group_concat; we'd rather avoid dialect-specific SQL here).
    table_ids = [t.id for t in tables]
    col_counts: dict[str, int] = {}
    col_names: dict[str, list[str]] = {}
    if table_ids:
        from sqlalchemy import func
        count_rows = (
            db.query(KBColumnMeta.table_meta_id, func.count(KBColumnMeta.id))
            .filter(KBColumnMeta.table_meta_id.in_(table_ids))
            .group_by(KBColumnMeta.table_meta_id)
            .all()
        )
        col_counts = {str(r[0]): r[1] for r in count_rows}

        name_rows = (
            db.query(KBColumnMeta.table_meta_id, KBColumnMeta.column_name)
            .filter(KBColumnMeta.table_meta_id.in_(table_ids))
            .all()
        )
        for tid, cname in name_rows:
            col_names.setdefault(str(tid), []).append(cname)

    return {
        "kb_id": kb_id,
        "kb_name": kb.name,
        "catalog_status": kb.catalog_status,
        "item_count": kb.item_count,
        "tables": [
            {
                "id": str(t.id),
                "schema_name": t.schema_name,
                "table_name": t.table_name,
                "table_type": t.table_type,
                "row_count": t.row_count,
                "column_count": col_counts.get(str(t.id), 0),
                "column_names": col_names.get(str(t.id), []),
                "description_zh": t.description_zh,
                "description_en": t.description_en,
                "indexed_at": t.indexed_at.isoformat() if t.indexed_at else None,
            }
            for t in tables
        ],
    }


@router.get("/{kb_id}/catalog/domains")
async def list_catalog_domains(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Return the discovered business domains for a database KnowledgeBase.

    This is the semantic layer's grouping output: each domain carries its
    LLM-derived name (en/zh), description, how it was produced
    (``method``: llm | derived) and its member tables with their per-column
    semantic typing (measure / dimension / temporal / identifier / flag), enum
    dictionaries and units.
    """
    from app.models.knowledge_catalog import (
        KBDomain,
        KBDomainMember,
        KBColumnMeta,
        KBTableMeta,
    )

    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")

    domains = (
        db.query(KBDomain)
        .filter(KBDomain.kb_id == kb_id, KBDomain.is_deleted == False)  # noqa: E712
        .order_by(KBDomain.member_count.desc(), KBDomain.name_en)
        .all()
    )
    members = (
        db.query(KBDomainMember)
        .filter(KBDomainMember.kb_id == kb_id)
        .all()
    )
    by_domain: dict[str, list[KBDomainMember]] = {}
    for m in members:
        by_domain.setdefault(m.domain_id, []).append(m)

    # Column semantics for the member tables (one query, grouped in Python).
    meta_ids = [m.table_meta_id for m in members]
    col_index: dict[str, dict] = {}
    if meta_ids:
        cols = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id.in_(meta_ids))
            .all()
        )
        for c in cols:
            entry = col_index.setdefault(c.table_meta_id, {"measures": [], "dimensions": [], "temporals": [], "enums": {}})
            label = c.description_en or c.description_zh or c.column_name
            if c.semantic_type == "measure":
                entry["measures"].append({"column": c.column_name, "unit": c.unit, "description": label})
            elif c.semantic_type == "temporal":
                entry["temporals"].append(c.column_name)
            elif c.semantic_type == "dimension":
                entry["dimensions"].append(c.column_name)
                if c.enum_values:
                    entry["enums"][c.column_name] = c.enum_values

    roles = {
        t.id: t.table_role
        for t in db.query(KBTableMeta)
        .filter(KBTableMeta.id.in_(meta_ids or [""]))
        .all()
    }

    return {
        "kb_id": kb_id,
        "kb_name": kb.name,
        "catalog_status": kb.catalog_status,
        "domain_count": len(domains),
        "domains": [
            {
                "id": d.id,
                "name_en": d.name_en,
                "name_zh": d.name_zh,
                "description_en": d.description_en,
                "description_zh": d.description_zh,
                "member_count": d.member_count,
                "method": d.method,
                "indexed_at": d.indexed_at.isoformat() if d.indexed_at else None,
                "tables": [
                    {
                        "table_name": m.table_name,
                        "table_meta_id": m.table_meta_id,
                        "table_role": roles.get(m.table_meta_id),
                        "measures": col_index.get(m.table_meta_id, {}).get("measures", []),
                        "dimensions": col_index.get(m.table_meta_id, {}).get("dimensions", []),
                        "date_columns": col_index.get(m.table_meta_id, {}).get("temporals", []),
                        "value_dictionary": col_index.get(m.table_meta_id, {}).get("enums", {}),
                    }
                    for m in sorted(by_domain.get(d.id, []), key=lambda x: x.table_name)
                ],
            }
            for d in domains
        ],
    }


@router.patch("/{kb_id}/catalog/tables/{table_id}")
async def update_catalog_table(
    app_id: str,
    kb_id: str,
    table_id: str,
    body: dict,
    db: Session = Depends(get_db),
):
    """Update a catalog table's human-editable fields (description_zh/en).

    Only description_zh and description_en are writable.  Used by the
    Catalog Tables edit pencil to let users override LLM-generated
    descriptions without triggering a full reindex.  Also refreshes the
    table's ChromaDB embedding so agent retrieval sees the new text.
    """
    from app.models.knowledge_catalog import KBTableMeta
    from app.services.knowledge_graph.catalog_indexer import update_table_embedding

    table = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.id == table_id, KBTableMeta.kb_id == kb_id)
        .first()
    )
    if table is None:
        raise HTTPException(status_code=404, detail="Catalog table not found")

    allowed = {"description_zh", "description_en"}
    applied = []
    for field in allowed:
        if field in body:
            value = body[field]
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=400, detail=f"{field} must be a string or null")
            setattr(table, field, value)
            applied.append(field)
    if not applied:
        raise HTTPException(status_code=400, detail="No editable fields supplied")

    # OCI #1 (2026-09-22): a human edit is the strongest provenance — mark it
    # so the pipeline can trust this description above any LLM output.
    table.description_source = "human"
    table.description_confidence = 1.0

    db.commit()
    db.refresh(table)

    # Refresh ChromaDB embedding so semantic catalog sees the edit
    update_table_embedding(
        kb_id=kb_id,
        table_meta_id=table.id,
        table_name=table.table_name,
        description_zh=table.description_zh,
        description_en=table.description_en,
        row_count=table.row_count,
        table_type=table.table_type or "TABLE",
    )

    logger.info("catalog table %s edited: %s", table_id, applied)

    return {
        "id": str(table.id),
        "description_zh": table.description_zh,
        "description_en": table.description_en,
        "description_confidence": table.description_confidence,
        "indexed_at": table.indexed_at.isoformat() if table.indexed_at else None,
    }


@router.patch("/{kb_id}/catalog/columns/{column_id}")
async def update_catalog_column(
    app_id: str,
    kb_id: str,
    column_id: str,
    body: dict,
    db: Session = Depends(get_db),
):
    """Update a catalog column's human-editable fields (description_zh/en, unit).

    OCI #1 (2026-09-22): the onboarding LLM writes "未知" for columns it
    cannot name and nothing distinguished those guesses from knowledge.
    A human edit here is the strongest provenance: ``description_source``
    becomes 'human' and ``description_confidence`` 1.0, so the pipeline
    trusts it above any LLM output. The parent table's ChromaDB embeddings
    are refreshed so retrieval sees the corrected text.
    """
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
    from app.services.knowledge_graph.catalog_indexer import update_column_embedding

    col = (
        db.query(KBColumnMeta)
        .join(KBTableMeta, KBColumnMeta.table_meta_id == KBTableMeta.id)
        .filter(KBColumnMeta.id == column_id, KBTableMeta.kb_id == kb_id)
        .first()
    )
    if col is None:
        raise HTTPException(status_code=404, detail="Catalog column not found")

    table = db.query(KBTableMeta).filter(KBTableMeta.id == col.table_meta_id).first()

    allowed = {"description_zh", "description_en", "unit"}
    applied = []
    for field in allowed:
        if field in body:
            value = body[field]
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=400, detail=f"{field} must be a string or null")
            setattr(col, field, value)
            applied.append(field)
    if not applied:
        raise HTTPException(status_code=400, detail="No editable fields supplied")

    col.description_source = "human"
    col.description_confidence = 1.0
    db.commit()
    db.refresh(col)

    if table is not None:
        update_column_embedding(
            kb_id=kb_id,
            table_meta_id=table.id,
            table_name=table.table_name,
            column_meta_id=str(col.id),
            column_name=col.column_name,
            description_zh=col.description_zh,
            description_en=col.description_en,
        )

    logger.info("catalog column %s edited: %s", column_id, applied)

    return {
        "id": str(col.id),
        "column_name": col.column_name,
        "description_zh": col.description_zh,
        "description_en": col.description_en,
        "unit": col.unit,
        "description_confidence": col.description_confidence,
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
    # Drop the schema cache first so the agent sees the freshly discovered
    # tables immediately (user explicitly asked for a re-scan).
    try:
        from app.services.db.schema_service import invalidate_schema_cache
        invalidate_schema_cache(kb_id)
    except Exception:
        pass
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
