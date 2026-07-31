"""Dashboards API — session-auth, org-scoped, fail-closed."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required
from app.models.dashboard import Dashboard
from app.models.agent_conversation import AgentConversation
from app.services.tool_handlers.dashboard_tools import _push_version
from app.services.dashboard_query import (
    validate_widget_sql, clamp_refresh_interval, run_dashboard_query,
    _run_single_sql,
)

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
