"""create_dashboard agent tool — builds a live dashboard bound to one DB KB.

Upfront pydantic validation closes the 'no upfront tool-arg validation' gap.
The datasource is resolved against the user's org (and project when known) to
prevent cross-project/cross-org IDOR. Stored widget SQL is re-validated at
query time (never trusted) by the /query endpoint.
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.models.dashboard import Dashboard
from app.models.knowledge_base import KnowledgeBase
from app.services.dashboard_query import validate_widget_sql, clamp_refresh_interval
from app.services import access_policy_service

logger = logging.getLogger(__name__)

ALLOWED_WIDGET_TYPES = {
    "kpi", "line", "bar", "pie", "table",
    "area", "stacked-bar", "scatter", "gauge", "radar",
}

MAX_VERSIONS = 20


def _push_version(db, dashboard, source: str, summary: str | None = None) -> None:
    """Snapshot the dashboard's CURRENT config as a new version (BEFORE an edit).

    Called right before mutating the dashboard. The snapshot captures the
    pre-edit state so a later undo restores it. History is capped at
    MAX_VERSIONS per dashboard (oldest pruned). Does not commit — the caller's
    commit flushes the version atomically with the edit.
    """
    from app.models.dashboard_version import DashboardVersion
    last = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == dashboard.id
    ).order_by(DashboardVersion.version_number.desc()).first()
    next_no = (last.version_number + 1) if last else 1
    db.add(DashboardVersion(
        dashboard_id=dashboard.id,
        version_number=next_no,
        config={
            "name": dashboard.name,
            "description": dashboard.description,
            "refresh_interval_seconds": dashboard.refresh_interval_seconds,
            "definition": dashboard.definition,
        },
        source=source,
        summary=summary,
    ))
    # Prune oldest beyond cap.
    all_v = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == dashboard.id
    ).order_by(DashboardVersion.version_number.desc()).all()
    for old in all_v[MAX_VERSIONS:]:
        db.delete(old)


class WidgetSpec(BaseModel):
    id: str
    type: str  # kpi | line | bar | pie | table | area | stacked-bar | scatter | gauge | radar
    title: str
    sql: str
    options: dict = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in ALLOWED_WIDGET_TYPES:
            raise ValueError(f"unknown widget type: {v!r}")
        return v

    @field_validator("sql")
    @classmethod
    def _readonly(cls, v: str) -> str:
        validate_widget_sql(v)
        return v

    @model_validator(mode="after")
    def _validate_drill(self):
        drill = (self.options or {}).get("drill")
        if drill:
            if not drill.get("sql") or not drill.get("value_column"):
                raise ValueError("options.drill requires 'sql' and 'value_column'")
            validate_widget_sql(drill["sql"])
        return self


class CreateDashboardArgs(BaseModel):
    datasource_id: str
    title: str
    widgets: list[WidgetSpec] = Field(min_length=1)
    refresh_interval_seconds: Optional[int] = None
    description: Optional[str] = None


def _user_can_access_kb(
    db, user_id, org_id, project_id, kb_id,
    resource_type: str | None = None, resource_id: str | None = None,
) -> KnowledgeBase | None:
    """Resolve the KB and verify it belongs to the user's org.

    When a project context is known, additionally require an id-OR-name project
    match (the dual-column convention used across zhanlu). Without a project
    context (chat without an active project) org-scoping alone is sufficient.

    When *resource_type*/*resource_id* are provided (injected into the tool
    context by ``prepare_data_source_runtime``), the user-level data access
    policy is also enforced: a KB the shared user is fully denied on returns
    ``None``.
    """
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.org_id == org_id,
        KnowledgeBase.is_deleted == False,  # noqa: E712
    ).first()
    if kb is None:
        return None
    if project_id:
        if not (kb.project_id and str(kb.project_id) == str(project_id)):
            from app.models.project import Project
            proj = db.query(Project).filter(
                Project.id == project_id, Project.org_id == org_id
            ).first()
            if not (proj and (kb.project or "").lower() == (proj.name or "").lower()):
                return None

    # Per-user data access policy: block dashboards bound to a fully-denied KB.
    if resource_type and resource_id and user_id:
        policy = access_policy_service.resolve_user_policies(
            db,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            bound_kb_ids=[kb_id],
        )
        if policy.is_kb_fully_denied(kb_id):
            logger.info(
                "create_dashboard: KB %s denied for user %s", kb_id, user_id
            )
            return None

    return kb


async def _create_dashboard(args: dict, db, user_id, context: dict | None = None) -> dict:
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    project_id = ctx.get("project_id")
    try:
        parsed = CreateDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid dashboard definition: {e}"}

    kb = _user_can_access_kb(
        db, user_id, org_id, project_id, parsed.datasource_id,
        resource_type=ctx.get("resource_type"),
        resource_id=ctx.get("resource_id"),
    )
    if kb is None:
        return {"success": False, "error": "Datasource not accessible in this project/org."}

    d = Dashboard(
        name=parsed.title,
        description=parsed.description,
        datasource_kb_id=kb.id,
        project_id=project_id,
        project=ctx.get("project_name") or "global",
        definition={"widgets": [w.model_dump() for w in parsed.widgets]},
        refresh_interval_seconds=clamp_refresh_interval(parsed.refresh_interval_seconds),
        org_id=org_id,
        app_id=ctx.get("app_id") or "default-app",
        created_by_id=user_id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    # Bind this dashboard to the active conversation (1:1). If the
    # conversation already had a bound dashboard, the new one replaces the
    # binding — the old dashboard is NOT deleted (still findable in MySpace).
    conv_id = ctx.get("conversation_id")
    if conv_id:
        from app.models.agent_conversation import AgentConversation
        conv = db.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
        if conv is not None:
            conv.dashboard_id = d.id
            db.commit()
    return {
        "success": True,
        "dashboard": d.to_dict(),
        "artifact": {
            "source": "dashboard",
            "dashboard_id": d.id,
            "title": d.name,
            "datasource_name": kb.name,
            "widget_count": len(parsed.widgets),
        },
    }


class UpdateDashboardArgs(BaseModel):
    """Args for editing an EXISTING dashboard. Only provided fields are applied.

    ``widgets`` (when provided) is the FULL updated widget list and replaces all
    widgets. ``datasource_kb_id`` is intentionally absent — it is immutable; to
    rebind, create a new dashboard.
    """

    dashboard_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    refresh_interval_seconds: Optional[int] = None
    widgets: Optional[list[WidgetSpec]] = None


async def _update_dashboard(args: dict, db, user_id, context: dict | None = None) -> dict:
    """Edit an existing dashboard (creator only). Provided fields replace.

    Mirrors ``_create_dashboard``'s return shape (``{success, dashboard,
    artifact}``). ``datasource_kb_id`` is never changed. WidgetSpec already
    runs ``validate_widget_sql`` on each widget + drill sql (read-only guard).
    """
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    try:
        parsed = UpdateDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid update args: {e}"}

    # Resolve the target dashboard: explicit dashboard_id wins; otherwise
    # fall back to the conversation's bound dashboard.
    target_id = parsed.dashboard_id
    if not target_id:
        conv_id = ctx.get("conversation_id")
        if conv_id:
            from app.models.agent_conversation import AgentConversation
            conv = db.query(AgentConversation).filter(
                AgentConversation.id == conv_id
            ).first()
            target_id = conv.dashboard_id if conv else None
    if not target_id:
        return {
            "success": False,
            "error": "No dashboard bound to this conversation. Call create_dashboard first, or pass dashboard_id explicitly.",
        }

    d = db.query(Dashboard).filter(
        Dashboard.id == target_id,
        Dashboard.org_id == org_id,
        Dashboard.is_deleted == False,  # noqa: E712
    ).first()
    if d is None:
        return {"success": False, "error": "Dashboard not found."}
    if d.created_by_id != user_id:
        return {"success": False, "error": "Only the dashboard creator can edit it."}

    # Snapshot the CURRENT (pre-edit) config as a version before mutating,
    # so a later undo can restore it. Skip when nothing is being changed
    # (avoids spurious version/undo entries on a no-op update call).
    _has_changes = any(
        v is not None
        for v in (parsed.title, parsed.description, parsed.refresh_interval_seconds, parsed.widgets)
    )
    if _has_changes:
        _push_version(db, d, source="agent",
                      summary="agent edit" if parsed.widgets else (parsed.title or "agent edit"))

    if parsed.title is not None:
        d.name = parsed.title
    if parsed.description is not None:
        d.description = parsed.description
    if parsed.refresh_interval_seconds is not None:
        d.refresh_interval_seconds = clamp_refresh_interval(parsed.refresh_interval_seconds)
    if parsed.widgets is not None:
        # WidgetSpec validates each widget's sql + drill (read-only guard) upfront.
        d.definition = {"widgets": [w.model_dump() for w in parsed.widgets]}
    # datasource_kb_id / org_id / app_id / project_id / project / created_by_id are immutable.
    db.commit()
    db.refresh(d)
    return {
        "success": True,
        "dashboard": d.to_dict(),
        "artifact": {
            "source": "dashboard",
            "dashboard_id": d.id,
            "title": d.name,
            "widget_count": len((d.definition or {}).get("widgets", [])),
        },
    }


# --- registration --------------------------------------------------------
CREATE_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_dashboard",
        "description": (
            "Build a LIVE dashboard bound to ONE database datasource. The dashboard "
            "auto-polls the database while open. Each widget needs read-only SQL "
            "(SELECT/WITH only). BEFORE calling this tool, confirm with the user: "
            "(1) datasource, (2) time window / duration, (3) key metrics & columns, "
            "(4) filter/drill dimensions, (5) chart intent. If ANY of these is "
            "ambiguous or unspecified, ask the user in ONE message with numbered "
            "options (e.g. 'A/B/C') and WAIT for their reply — do NOT guess. One "
            "dashboard binds to exactly ONE datasource; if the user has not named "
            "one, ask them to pick from the project's connected databases first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "datasource_id": {"type": "string", "description": "The chosen DB datasource (KnowledgeBase) id."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "refresh_interval_seconds": {"type": "integer", "description": "Poll interval, 10..300 (default 30)."},
                "widgets": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": ["kpi", "line", "bar", "pie", "table", "area", "stacked-bar", "scatter", "gauge", "radar"]},
                            "title": {"type": "string"},
                            "sql": {"type": "string", "description": "Read-only SELECT/WITH SQL for this widget."},
                            "options": {
                                "type": "object",
                                "description": (
                                    "Per-type options. kpi: {value_column} required; optional "
                                    "unit (suffix like '%', 'ms', 'USD'), compare_column (second "
                                    "numeric column -> delta chip), subtitle (context line), "
                                    "thresholds {direction:'higher_is_worse'|'lower_is_worse', "
                                    "warning:<n>, critical:<n>} -> green/amber/red coloring. "
                                    "line/bar: {x_column, y_column}. pie: {name_column, value_column}. "
                                    "table: {}. area: {x_column, y_column}. stacked-bar: {x_column, series:[{key,label}]}. "
                                    "scatter: {x_column, y_column, name_column?}. gauge: {value_column, min?, max?, target?, unit?}. "
                                    "radar: {axis_column, series:[{key,label}]}. ALL chart types accept optional "
                                    "motion ('none'|'subtle'|'lively', default 'subtle') controlling animation, and "
                                    "zoom (true/false) enabling a brush selector on area/line/bar/stacked-bar. "
                                    "ALL types: optional dimensions "
                                    "[{token,column}] enables cross-widget filtering via "
                                    ":dim_<token> tokens in sql (renders col='value' or 1=1). "
                                    "Optional drill {value_column, sql, title} enables click-to-"
                                    "drill; drill sql uses :drill_value. Date tokens :from/:to/:date "
                                    "in any widget sql bind to the viewer's date-range selector "
                                    "(default last-30-days)."
                                ),
                            },
                        },
                        "required": ["id", "type", "title", "sql"],
                    },
                },
            },
            "required": ["datasource_id", "title", "widgets"],
        },
    },
}

try:
    from app.services.tool_registry import registry
    registry.register(
        name="create_dashboard",
        schema=CREATE_DASHBOARD_SCHEMA,
        handler=_create_dashboard,
        category="dashboards",
        toolset="dashboards",
        description="Build a live database dashboard that auto-polls while open.",
        emoji="📊",
        max_result_size_chars=4_000,
    )
except Exception:
    logger.exception("Failed to register create_dashboard tool")


UPDATE_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_dashboard",
        "description": (
            "Edit an EXISTING live dashboard. Provide only the fields to change: "
            "title, description, refresh_interval_seconds, and/or widgets (FULL "
            "updated widget list — replaces all widgets). Fetch the current "
            "dashboard first (list_dashboards / get detail) to know existing "
            "widgets, then send the full updated list. datasource_kb_id CANNOT "
            "be changed (to rebind, create a new dashboard). Only the dashboard's "
            "CREATOR can edit it. If the user's edit intent is ambiguous (which "
            "widget, what change, new SQL), ask in ONE message with numbered "
            "options and WAIT — do NOT guess. Widget SQL must be read-only "
            "(SELECT/WITH only, single statement). If dashboard_id is omitted, "
            "the dashboard bound to the current conversation is edited automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dashboard_id": {"type": "string", "description": "The id of the dashboard to edit. OPTIONAL: when omitted, the dashboard bound to the active conversation is used."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "refresh_interval_seconds": {"type": "integer", "description": "Poll interval, 10..300."},
                "widgets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": ["kpi", "line", "bar", "pie", "table", "area", "stacked-bar", "scatter", "gauge", "radar"]},
                            "title": {"type": "string"},
                            "sql": {"type": "string", "description": "Read-only SELECT/WITH SQL for this widget."},
                            "options": {"type": "object"},
                        },
                        "required": ["id", "type", "title", "sql"],
                    },
                },
            },
            "required": [],
        },
    },
}

try:
    from app.services.tool_registry import registry
    registry.register(
        name="update_dashboard",
        schema=UPDATE_DASHBOARD_SCHEMA,
        handler=_update_dashboard,
        category="dashboards",
        toolset="dashboards",
        description="Edit an existing live dashboard (creator only).",
        emoji="✏️",
        max_result_size_chars=4_000,
    )
except Exception:
    logger.exception("Failed to register update_dashboard tool")


# --- undo -----------------------------------------------------------------
class UndoDashboardEditArgs(BaseModel):
    dashboard_id: Optional[str] = None  # optional: resolves from conversation


async def _undo_dashboard_edit(args: dict, db, user_id, context: dict | None = None) -> dict:
    """Undo the last edit on the conversation's bound dashboard (or explicit id)."""
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    try:
        parsed = UndoDashboardEditArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid undo args: {e}"}

    target_id = parsed.dashboard_id
    if not target_id:
        conv_id = ctx.get("conversation_id")
        if conv_id:
            from app.models.agent_conversation import AgentConversation
            conv = db.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
            target_id = conv.dashboard_id if conv else None
    if not target_id:
        return {"success": False, "error": "No dashboard bound to this conversation to undo."}

    from app.models.dashboard import Dashboard
    from app.models.dashboard_version import DashboardVersion
    d = db.query(Dashboard).filter(
        Dashboard.id == target_id, Dashboard.org_id == org_id, Dashboard.is_deleted == False,  # noqa: E712
    ).first()
    if d is None:
        return {"success": False, "error": "Dashboard not found."}
    if d.created_by_id != user_id:
        return {"success": False, "error": "Only the dashboard creator can undo edits."}
    latest = db.query(DashboardVersion).filter(
        DashboardVersion.dashboard_id == d.id
    ).order_by(DashboardVersion.version_number.desc()).first()
    if latest is None:
        return {"success": False, "error": "No edit history to undo."}
    _push_version(db, d, source="undo", summary="undo")
    cfg = latest.config
    d.name = cfg.get("name", d.name)
    d.description = cfg.get("description", d.description)
    d.refresh_interval_seconds = cfg.get("refresh_interval_seconds", d.refresh_interval_seconds)
    d.definition = cfg.get("definition", d.definition)
    db.delete(latest)
    db.commit()
    db.refresh(d)
    return {
        "success": True,
        "dashboard": d.to_dict(),
        "artifact": {
            "source": "dashboard",
            "dashboard_id": d.id,
            "title": d.name,
            "widget_count": len((d.definition or {}).get("widgets", [])),
            "undone": True,
        },
    }


UNDO_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "undo_dashboard_edit",
        "description": (
            "Undo the last edit made to the dashboard bound to the current "
            "conversation (or an explicit dashboard_id). Restores the previous "
            "widget list, title, and layout. Use when the user says 'undo "
            "that' or 'revert'. Can be called repeatedly to step back through "
            "edit history."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dashboard_id": {"type": "string", "description": "Optional. Defaults to the conversation's bound dashboard."},
            },
            "required": [],
        },
    },
}

try:
    from app.services.tool_registry import registry
    registry.register(
        name="undo_dashboard_edit",
        schema=UNDO_DASHBOARD_SCHEMA,
        handler=_undo_dashboard_edit,
        category="dashboards",
        toolset="dashboards",
        description="Undo the last edit on the bound dashboard.",
        emoji="\u21b6",
        max_result_size_chars=2_000,
    )
except Exception:
    logger.exception("Failed to register undo_dashboard_edit tool")
