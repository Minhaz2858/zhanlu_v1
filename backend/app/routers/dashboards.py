"""Dashboards API — session-auth, org-scoped, fail-closed."""
import importlib
import logging
import mimetypes
import uuid
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required
from app.models.dashboard import Dashboard
from app.models.agent_conversation import AgentConversation
from app.services.auth_service import auth_service
from app.services.tool_handlers.dashboard_tools import _push_version
from app.services.dashboard_query import (
    validate_widget_sql, clamp_refresh_interval, run_dashboard_query,
    _run_single_sql,
)
from app.services.dashboard_app.manager import dashboard_app_manager
from app.services.dashboard_app.generator import module_name
from app.models.dashboard_app import DashboardApp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


class QueryParams(BaseModel):
    """Optional query-time params for the /query endpoint.

    ``from_``/``to`` drive the :from/:to tokens (default last-30-days).
    ``filters`` drives :dim_<token> cross-widget filtering. ``drill`` triggers
    a widget's options.drill.sql with :drill_value set. All fields optional —
    an empty or absent body reproduces the current (tokenless) behavior.
    """

    from_: str | None = None
    to: str | None = None
    filters: dict[str, str] | None = None
    drill: dict | None = None


def _scoped(db: Session, user, dashboard_id: str) -> Dashboard:
    d = db.query(Dashboard).filter(
        Dashboard.id == dashboard_id,
        Dashboard.org_id == user.org_id,
        Dashboard.is_deleted == False,  # noqa: E712
    ).first()
    if d is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return d


def _validate_definition(definition: dict) -> None:
    if (not isinstance(definition, dict)
            or not isinstance(definition.get("widgets"), list)
            or not definition["widgets"]):
        raise HTTPException(status_code=400, detail="definition.widgets must be a non-empty list")
    for w in definition["widgets"]:
        if not w.get("sql"):
            raise HTTPException(status_code=400, detail="each widget needs sql")
        try:
            validate_widget_sql(w["sql"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"widget {w.get('id')}: {e}")
        drill = (w.get("options") or {}).get("drill")
        if drill:
            if not drill.get("sql") or not drill.get("value_column"):
                raise HTTPException(status_code=400, detail=f"widget {w.get('id')}: options.drill requires 'sql' and 'value_column'")
            try:
                validate_widget_sql(drill["sql"])
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"widget {w.get('id')}: drill sql: {e}")


def _with_can_edit(db: Session, d: Dashboard, user) -> dict:
    """Serialize a dashboard with UI gates (``can_edit`` and ``can_undo``).

    The frontend has no client-side user-identity context, so the server
    computes whether this user may edit (creator-only) and ships it as
    ``can_edit``. ``can_undo`` indicates whether there is at least one
    DashboardVersion snapshot available to undo (edit history exists to step
    back through). The PATCH/preview/tool/undo endpoints still enforce
    creator-only independently (fail-closed) — these flags are purely for UI.
    """
    from app.models.dashboard_version import DashboardVersion
    data = d.to_dict()
    data["can_edit"] = d.created_by_id == user.id
    has_versions = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == d.id
    ).count() > 0
    data["can_undo"] = has_versions
    return data


@router.post("", status_code=201)
def create_dashboard(payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    _validate_definition(payload.get("definition") or {})
    d = Dashboard(
        name=payload["name"],
        description=payload.get("description"),
        datasource_kb_id=payload["datasource_kb_id"],
        project_id=payload.get("project_id"),
        project=payload.get("project") or "global",
        definition=payload["definition"],
        refresh_interval_seconds=clamp_refresh_interval(payload.get("refresh_interval_seconds")),
        org_id=user.org_id,
        app_id=getattr(user, "app_id", "default-app"),
        created_by_id=user.id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return _with_can_edit(db, d, user)


@router.get("")
def list_dashboards(project_id: str | None = Query(None),
                    db: Session = Depends(get_db),
                    user=Depends(get_current_user_required)):
    q = db.query(Dashboard).filter(
        Dashboard.org_id == user.org_id,
        Dashboard.is_deleted == False,  # noqa: E712
    )
    if project_id:
        q = q.filter(Dashboard.project_id == project_id)
    return [_with_can_edit(db, d, user) for d in q.order_by(Dashboard.updated_date.desc()).limit(1000)]


# ── Full-stack dashboard app records ──
# NOTE: the generated app *frontends* are served as static files at
# `/api/dashboards/apps/{slug}/` (mounted by DashboardAppManager). These
# JSON record endpoints use the `/app-records/` prefix to avoid colliding
# with that static mount. IMPORTANT: these must be registered BEFORE the
# legacy `/{dashboard_id}` route below — FastAPI matches in registration
# order, and a bare `/app-records` would otherwise be swallowed by it.
def _user_can_see_record(record: DashboardApp, user) -> bool:
    """T10 scope visibility: company → whole org; personal → creator only."""
    if record.scope == "company":
        return True
    return str(record.created_by_id) == str(getattr(user, "id", None))


@router.get("/app-records/{slug_or_id}")
def get_dashboard_app_record(slug_or_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """Return the DashboardApp record for a full-stack dashboard.

    Accepts either the record **UUID** (what the frontend puts in
    ``/dashboard/:id`` and passes straight through to this endpoint) or the
    **slug** (``sales-performance-dashboard``). Resolving by id is the
    primary path — the frontend has no slug at navigation time, only the
    record id from the app-records list.

    Org-scoped + T10 scope visibility. 404 when the record does not exist,
    belongs to another org, or is a personal app owned by someone else.
    The frontend uses this to decide whether ``/dashboard/:id`` is a
    full-stack app (render iframe) vs a legacy SQL-widget dashboard.
    """
    record = db.query(DashboardApp).filter(
        or_(
            DashboardApp.slug == slug_or_id,
            DashboardApp.id == slug_or_id,
        ),
        DashboardApp.org_id == user.org_id,
    ).first()
    if record is None or not _user_can_see_record(record, user):
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    return _record_with_meta(record)


@router.get("/app-records")
def list_dashboard_app_records(db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """List full-stack dashboard app records visible to the current user.

    T10 scope rule: ``company`` apps are visible to the whole org; ``personal``
    apps only to their creator.
    """
    records = dashboard_app_manager.list_apps(org_id=user.org_id, user_id=user.id)
    return [_record_with_meta(r) for r in records]


def _record_with_meta(record: DashboardApp) -> dict:
    """Serialize a DashboardApp record + derived fields.

    ``unread`` is derived from the realtime poller's ``last_data_change_at``
    vs the user's last ``viewed_at``: data changed after the user last opened
    the dashboard → unread. NULL ``last_data_change_at`` (no change event yet)
    is never unread.

    ``scope`` (T10) lets the frontend classify into From Personal / From
    Company tabs in My Files.
    """
    data = record.to_dict()
    data["app_url"] = data.get("app_url") or f"/api/dashboards/apps/{record.slug}/"
    data["scope"] = record.scope or "personal"
    # Compare on the ORM datetime objects (to_dict() emits ISO strings).
    last_change = record.last_data_change_at
    viewed = record.viewed_at
    data["unread"] = bool(
        last_change is not None
        and (viewed is None or last_change > viewed)
    )
    return data


@router.get("/app-records/{slug}/chat-thread")
def get_dashboard_app_chat_thread(slug: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """Return the chat conversation that created a dashboard app, or 404.

    T5 artifact linking: My Files "Open in chat" needs the AgentConversation id
    (``chat_thread_id``) to deep-link via ``/?conv=<id>``. Org-scoped; 404 when
    the record is missing or was built outside a chat (no thread bound).
    """
    record = db.query(DashboardApp).filter(
        DashboardApp.slug == slug,
        DashboardApp.org_id == user.org_id,
    ).first()
    if record is None or not _user_can_see_record(record, user):
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    if not record.chat_thread_id:
        raise HTTPException(status_code=404, detail="No chat thread bound to this dashboard")
    return {"slug": slug, "chat_thread_id": record.chat_thread_id}


@router.post("/app-records/{slug}/mark-viewed")
def mark_dashboard_app_viewed(slug: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """Clear the unread badge: record when the user last opened the dashboard.

    Idempotent. Org-scoped like the rest of the app-records API. Returns the
    updated record so the frontend can refresh the My Files list without a
    second round-trip.
    """
    from datetime import datetime, timezone

    record = db.query(DashboardApp).filter(
        DashboardApp.slug == slug,
        DashboardApp.org_id == user.org_id,
    ).first()
    if record is None or not _user_can_see_record(record, user):
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    record.viewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return _record_with_meta(record)


def _is_valid_uuid(s: str) -> bool:
    """True when ``s`` parses as a UUID (used to disambiguate slug vs id)."""
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


@router.delete("/app-records/{slug_or_id}", status_code=204)
def delete_dashboard_app_record(slug_or_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """Hard-delete a full-stack dashboard app record and its artifacts.

    Resolves by record UUID or slug (org-scoped + T10 visibility: personal →
    creator only; company → whole org, but non-creators need the ``admin``
    role). When the value parses as a UUID it is treated as a record id (a
    slug that happens to be UUID-shaped is ambiguous; the id match wins). On
    success the cascade removes: the realtime poller task, the generated app
    directory on disk, chat conversations bound via ``metadata_`` (see
    ``delete_bound_conversations``), and finally the record itself. Returns
    204 with no body.

    Multi-worker note: ``stop_poller`` is per-process — in a multi-worker
    deployment a poller running on ANOTHER worker may keep running until its
    poll loop notices the record is gone from the DB. Acceptable for now;
    logged for awareness.
    """
    # UUID-vs-slug resolution: or_(slug == x, id == x) + .first() is
    # nondeterministic when a slug is UUID-shaped, so prefer the id match
    # when the value parses as a UUID, else match on slug. Org scope kept.
    if _is_valid_uuid(slug_or_id):
        record = db.query(DashboardApp).filter(
            DashboardApp.id == slug_or_id,
            DashboardApp.org_id == user.org_id,
        ).first()
    else:
        record = db.query(DashboardApp).filter(
            DashboardApp.slug == slug_or_id,
            DashboardApp.org_id == user.org_id,
        ).first()
    if record is None or not _user_can_see_record(record, user):
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    # Company-scope gate: anyone but the creator needs the admin role.
    if record.scope == "company" and str(record.created_by_id) != str(user.id):
        if getattr(user, "role", "user") != "admin":
            raise HTTPException(status_code=404, detail="Dashboard app not found")
    slug = record.slug
    # Stop the realtime poller (best effort — the task may not be running).
    try:
        dashboard_app_manager.stop_poller(slug)
    except Exception:
        logger.exception("delete: failed to stop poller for %s", slug)
    # Remove the generated app directory from disk (best effort).
    try:
        import shutil
        from app.services.dashboard_app.generator import get_generator
        app_dir = get_generator().app_dir(slug)
        if app_dir.exists():
            shutil.rmtree(app_dir, ignore_errors=True)
    except Exception:
        logger.exception("delete: failed to remove app dir for %s", slug)
    # Cascade: drop chat conversations bound to this dashboard.
    from app.services.dashboard_app.cascade import delete_bound_conversations
    deleted = delete_bound_conversations(db, slug, record.id)
    if deleted == 0:
        logger.warning(
            "delete: no bound conversations cascaded for dashboard app %s "
            "(slug=%s) — chats bound via metadata_ may be orphaned",
            record.id, slug,
        )
    db.delete(record)
    db.commit()
    return None


# ── Full-stack dashboard app serving (T17) ──
# These GLOBAL catch-all routes replace the per-app `app.mount(StaticFiles)`
# that DashboardAppManager used to install at startup.  The old mount was
# in-memory + per-worker: a file uploaded/created after startup (or on a
# different worker than the one serving the request) could never be fetched,
# so the viewer's `config.json` fetch 404'd and the WS badge sat on
# "Reconnecting…" forever.
#
# The routes below resolve the app directory / generated module from disk+DB on
# EVERY request, so they work on any worker, for any app, without a restart.
# They are registered BEFORE the legacy `/{dashboard_id}` route (FastAPI
# matches in registration order) so `/apps/...` never collides with it.
def _resolve_module(slug: str):
    """Import the generated module `app.dashboards.{slug}.api` for a slug.

    Returns the module object (cached by importlib) or raises ImportError when
    the app source directory does not exist / has not been generated yet.
    """
    return importlib.import_module(f"app.dashboards.{module_name(slug)}.api")


def _media_type_for(name: str) -> str:
    """Best-effort Content-Type for a served dashboard static file."""
    mt, _ = mimetypes.guess_type(name)
    if name.endswith(".js") or name.endswith(".mjs"):
        return "application/javascript"
    if name.endswith(".css"):
        return "text/css"
    if name.endswith(".json"):
        return "application/json"
    return mt or "application/octet-stream"


@router.get("/apps/{slug}/metrics")
def app_metrics(slug: str):
    """List the metrics exposed by a generated dashboard app.

    Resolves the generated module per-request (any worker, post-startup apps).
    Restores the `/config`+`/metrics` REST surface the old mount used to
    provide; the frontend app calls `./metrics/{id}` for the initial snapshot.
    """
    try:
        mod = _resolve_module(slug)
    except ImportError:
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    if dashboard_app_manager.get_app(slug) is None:
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    metrics = getattr(mod, "METRICS", [])
    return {"metrics": [{"id": m["id"], "title": m["title"], "type": m["type"]} for m in metrics]}


@router.get("/apps/{slug}/metrics/{metric_id}")
async def app_metric(slug: str, metric_id: str, request: Request, db: Session = Depends(get_db)):
    """Return the live payload for one metric of a generated dashboard app.

    Async endpoint: ``run_metric`` is a coroutine that must be awaited on the
    event loop. The previous sync version called
    ``asyncio.get_event_loop().run_until_complete()`` from FastAPI's threadpool
    thread — where no event loop exists — so every widget 500'd and the
    frontend rendered "No data" on all cards. ``db`` is required because
    ``QueryService`` resolves the datasource KB through the SQLAlchemy session.
    """
    try:
        mod = _resolve_module(slug)
    except ImportError:
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    if dashboard_app_manager.get_app(slug) is None:
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    filters = dict(request.query_params)
    try:
        result = await mod.run_metric(db, metric_id, filters)
    except Exception as exc:  # metric not found / query error → surface cleanly
        logger.warning("dashboard app %s metric %s failed: %s", slug, metric_id, exc)
        raise HTTPException(status_code=404, detail=f"metric not found: {metric_id}")
    return {"metric_id": metric_id, "data": result}


@router.get("/apps/{slug}/{path:path}")
def serve_dashboard_app(slug: str, path: str):
    """Serve a generated dashboard app's static `dist/` files.

    Resolves the on-disk app directory on every request (works on any worker,
    any app, no restart).  Guards against `..` traversal and falls back to
    `index.html` for client-side routes (SPA).  Missing app/slug → 404 JSON;
    traversal → 400 JSON; missing file → 404 JSON.  Never raises 5xx for a
    benign bad request.
    """
    record = dashboard_app_manager.get_app(slug)
    if record is None:
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    app_dir = dashboard_app_manager.resolve_app_dir(slug)
    if app_dir is None or not app_dir.exists():
        raise HTTPException(status_code=404, detail="Dashboard app files missing on disk")

    # `config.json` is generated at the app ROOT (not in dist/), but the
    # frontend fetches it at the app URL root. Serve it from the root when
    # present, otherwise fall through to the dist/ lookup below.
    if path == "config.json" and (app_dir / "config.json").exists():
        safe = (app_dir / "config.json").resolve()
        return FileResponse(
            safe,
            media_type=_media_type_for(safe.name),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    # The built SPA lives in the app's `dist/` directory; assets + index.html
    # are served from there. SPA client routes fall back to index.html.
    base = (app_dir / "dist").resolve()
    if not base.exists():
        raise HTTPException(status_code=404, detail="Dashboard app files missing on disk")
    safe = (base / path).resolve()
    if not safe.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid path")
    if safe.is_dir():
        safe = safe / "index.html"
    elif not safe.exists() and "." not in safe.name:
        safe = base / "index.html"  # SPA fallback for client routes
    if not safe.exists():
        raise HTTPException(status_code=404, detail="Not found")
    # Cache policy: the SPA shell (index.html) MUST revalidate on every load so
    # a redeploy's new hashed bundle reference is picked up — without this,
    # browsers heuristically cache index.html and keep requesting the OLD
    # bundle name (which 404s or serves stale JS) after a rebuild. Hashed
    # assets are content-addressed (index-<hash>.js/css) so they are safe to
    # cache immutably.
    headers = {}
    if safe.name == "index.html":
        headers["Cache-Control"] = "no-cache, must-revalidate"
    else:
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return FileResponse(safe, media_type=_media_type_for(safe.name), headers=headers)


@router.websocket("/apps/{slug}/ws")
async def app_ws(websocket: WebSocket, slug: str):
    """Proxy the generated app's live WebSocket.

    The generated module owns the `ConnectionManager` + poller; we resolve it
    per-connect (not at process start) so any worker can serve the socket
    regardless of which worker started the poller.  Anonymous/invalid tokens
    are rejected with close code 1008 (T9 auth).
    """
    token = websocket.query_params.get("token")
    if not token or not auth_service.verify_token(token):
        await websocket.close(code=1008, reason="invalid token")
        return
    try:
        mod = _resolve_module(slug)
    except ImportError:
        await websocket.close(code=1008, reason="app not found")
        return
    if dashboard_app_manager.get_app(slug) is None:
        await websocket.close(code=1008, reason="app not found")
        return
    mgr = mod.get_connection_manager()
    await mgr.connect(slug, websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / disconnect detect
    except Exception:
        pass
    finally:
        mgr.disconnect(slug, websocket)


@router.get("/{dashboard_id}")
def get_dashboard(dashboard_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    return _with_can_edit(db, _scoped(db, user, dashboard_id), user)


@router.get("/by-conversation/{conversation_id}")
def get_dashboard_by_conversation(conversation_id: str,
                                  db: Session = Depends(get_db),
                                  user=Depends(get_current_user_required)):
    """Return the dashboard bound to a conversation, or 404.

    Used by the chat UI to reopen the docked panel on conversation load.
    Org-scoped via the AgentConversation.org_id wall.
    """
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.org_id == user.org_id,
    ).first()
    if conv is None or not conv.dashboard_id:
        raise HTTPException(status_code=404, detail="No dashboard bound to this conversation")
    return _with_can_edit(db, _scoped(db, user, conv.dashboard_id), user)


@router.delete("/{dashboard_id}", status_code=204)
def delete_dashboard(dashboard_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    d.is_deleted = True
    db.commit()
    return None


class UpdateDashboardBody(BaseModel):
    """Partial update for a dashboard. Only provided fields are applied.

    ``datasource_kb_id`` is deliberately absent — rebinding a dashboard to a
    different KB would invalidate all widget SQL and is an IDOR risk, so it is
    immutable. A caller sending it is ignored (not an error).
    """

    name: str | None = None
    description: str | None = None
    refresh_interval_seconds: int | None = None
    definition: dict | None = None


@router.patch("/{dashboard_id}")
def update_dashboard(dashboard_id: str, body: UpdateDashboardBody,
                     db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    if d.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can edit this dashboard")
    # Snapshot the CURRENT (pre-edit) config as a version before mutating.
    # Skip on a no-op update to avoid spurious version/undo entries.
    _has_changes = any(v is not None for v in (body.name, body.description, body.refresh_interval_seconds, body.definition))
    if _has_changes:
        _push_version(db, d, source="manual", summary="manual edit")
    if body.definition is not None:
        _validate_definition(body.definition)
        d.definition = body.definition
    if body.refresh_interval_seconds is not None:
        d.refresh_interval_seconds = clamp_refresh_interval(body.refresh_interval_seconds)
    if body.name is not None:
        d.name = body.name
    if body.description is not None:
        d.description = body.description
    # datasource_kb_id / org_id / app_id / project_id / project / created_by_id are immutable.
    db.commit()
    db.refresh(d)
    return _with_can_edit(db, d, user)


@router.post("/{dashboard_id}/undo")
def undo_dashboard(dashboard_id: str,
                   db: Session = Depends(get_db),
                   user=Depends(get_current_user_required)):
    """Restore the dashboard's config to its state before the last edit.

    Pops the latest DashboardVersion, applies its config, and records a new
    version with source='undo' (so the undo itself can be reverted = redo).
    Creator-only. 409 when there is no version history.
    """
    from app.models.dashboard_version import DashboardVersion
    d = _scoped(db, user, dashboard_id)
    if d.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can undo edits on this dashboard")
    latest = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == d.id
    ).order_by(DashboardVersion.version_number.desc()).first()
    if latest is None:
        raise HTTPException(status_code=409, detail="No edit history to undo")
    # Snapshot the CURRENT (pre-undo) state as an "undo" version first.
    _push_version(db, d, source="undo", summary="undo")
    cfg = latest.config
    d.name = cfg.get("name", d.name)
    d.description = cfg.get("description", d.description)
    d.refresh_interval_seconds = cfg.get("refresh_interval_seconds", d.refresh_interval_seconds)
    d.definition = cfg.get("definition", d.definition)
    db.delete(latest)  # the restored version is consumed
    db.commit()
    db.refresh(d)
    return _with_can_edit(db, d, user)


@router.post("/{dashboard_id}/query")
async def query_dashboard(dashboard_id: str, body: QueryParams | None = None,
                          db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    params = None
    if body is not None:
        params = {}
        if body.from_:
            params["from"] = body.from_
        if body.to:
            params["to"] = body.to
        if body.filters:
            params["filters"] = body.filters
        if body.drill:
            params["drill"] = body.drill
    return await run_dashboard_query(db, d, params)


class PreviewSqlBody(BaseModel):
    """Live preview of arbitrary widget SQL against the bound KB (no persistence).

    Runs user-supplied SQL through the same read-only/single-statement guard +
    token render + QueryService row-cap/timeout as stored widget SQL, but does
    NOT persist anything. Creator-only (tighter than /query which runs stored
    SQL only).
    """

    sql: str
    params: dict | None = None


@router.post("/{dashboard_id}/preview-sql")
async def preview_widget_sql(dashboard_id: str, body: PreviewSqlBody,
                             db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    d = _scoped(db, user, dashboard_id)
    if d.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can preview SQL on this dashboard")
    # Read-only / single-statement guard -> 400 on violation (DELETE, multi-stmt, etc.).
    try:
        validate_widget_sql(body.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Resolve KB controls as upper bounds (same as run_dashboard_query).
    max_rows, timeout_s = 1000, 10
    try:
        from app.models.knowledge_base import KnowledgeBase
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == d.datasource_kb_id).first()
        if kb is not None:
            max_rows = int(getattr(kb, "max_rows_per_query", 1000) or 1000)
            timeout_s = int(getattr(kb, "timeout_seconds", 10) or 10)
    except Exception:
        pass
    # _run_single_sql renders tokens (unknown token -> error field), re-validates,
    # and executes; execution/DB errors are returned in `error` (HTTP 200), not 5xx.
    return await _run_single_sql(db, d.datasource_kb_id, body.sql, body.params or {}, [], max_rows, timeout_s)
