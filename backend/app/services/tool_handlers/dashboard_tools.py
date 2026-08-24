"""create_dashboard agent tool — builds a live dashboard bound to one DB KB.

Upfront pydantic validation closes the 'no upfront tool-arg validation' gap.
The datasource is resolved against the user's org (and project when known) to
prevent cross-project/cross-org IDOR. Stored widget SQL is re-validated at
query time (never trusted) by the /query endpoint.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.models.dashboard import Dashboard
from app.models.knowledge_base import KnowledgeBase
from app.services.dashboard_query import validate_widget_sql, clamp_refresh_interval
from app.services import access_policy_service
from app.config import settings

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

# Legacy SQL-widget pipeline. Kept behind LEGACY_DASHBOARD_ENABLED for emergency
# rollback — hidden from the default agent path (the new full-stack pipeline is
# the default when FULLSTACK_DASHBOARD_ENABLED=True).
if getattr(settings, "LEGACY_DASHBOARD_ENABLED", False):
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


# ═══════════════════════════════════════════════════════════════════════════
# Full-Stack Dashboard App Pipeline (new)
#
# Replaces the legacy SQL-widget create_dashboard flow. The agent submits a
# DashboardSpec; the generator fills Jinja2 templates → deployable FastAPI
# sub-router + pre-built React dist + WebSocket realtime layer. Design system
# comes from uiux_design_system(--persist). All flags gated by
# FULLSTACK_DASHBOARD_ENABLED (default False).
# ═══════════════════════════════════════════════════════════════════════════

import re as _re

FULLSTACK_ALLOWED_TYPES = {
    "kpi", "line", "bar", "pie", "table", "area", "gauge", "radar",
}

_SLUG_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _fullstack_dashboard_enabled() -> bool:
    try:
        from app.config import settings
        return bool(settings.FULLSTACK_DASHBOARD_ENABLED)
    except Exception:
        return False


class FullstackMetricSpec(BaseModel):
    """One dashboard widget. SQL is validated read-only at arg time."""

    id: str
    type: str  # kpi | line | bar | pie | table | area | gauge | radar
    title: str
    sql: str
    options: dict = Field(default_factory=dict)

    @field_validator("type")
    def _valid_type(cls, v: str) -> str:
        if v not in FULLSTACK_ALLOWED_TYPES:
            raise ValueError(f"type must be one of {sorted(FULLSTACK_ALLOWED_TYPES)}")
        return v

    @field_validator("sql")
    def _readonly_sql(cls, v: str) -> str:
        try:
            validate_widget_sql(v)
        except ValueError as exc:
            raise ValueError(f"invalid SQL: {exc}") from exc
        return v


class CreateFullstackDashboardArgs(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    datasource_id: str
    design_system_ref: Optional[str] = None
    metrics: list[FullstackMetricSpec] = Field(min_length=1)
    refresh_interval_seconds: int = 30
    theme: str = "light"
    # T10: "personal" (creator only) or "company" (whole org). The agent
    # should ask the user at creation time; falls back to the config default.
    scope: str = "personal"

    @field_validator("slug")
    def _slug_ok(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must be 1-64 chars, lowercase [a-z0-9_-], starting with a letter/digit"
            )
        return v

    @field_validator("theme")
    def _theme_ok(cls, v: str) -> str:
        if v not in ("light", "dark"):
            raise ValueError("theme must be 'light' or 'dark'")
        return v

    @field_validator("scope")
    def _scope_ok(cls, v: str) -> str:
        if v not in ("personal", "company"):
            raise ValueError("scope must be 'personal' or 'company'")
        return v


class UpdateFullstackDashboardArgs(BaseModel):
    """Only provided fields are applied to the existing app's spec."""

    slug: str
    name: Optional[str] = None
    description: Optional[str] = None
    design_system_ref: Optional[str] = None
    metrics: Optional[list[FullstackMetricSpec]] = None
    refresh_interval_seconds: Optional[int] = None
    theme: Optional[str] = None

    @field_validator("slug")
    def _slug_ok(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("slug must be lowercase [a-z0-9_-], 1-64 chars")
        return v

    @field_validator("theme")
    def _theme_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("light", "dark"):
            raise ValueError("theme must be 'light' or 'dark'")
        return v


class RevertFullstackDashboardArgs(BaseModel):
    slug: str

    @field_validator("slug")
    def _slug_ok(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("slug must be lowercase [a-z0-9_-], 1-64 chars")
        return v


async def _create_fullstack_dashboard(
    args: dict, db, user_id, context: Optional[dict] = None,
) -> dict:
    """Deploy a full-stack dashboard app from a DashboardSpec."""
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    project_id = ctx.get("project_id")
    if not _fullstack_dashboard_enabled():
        return {
            "success": False,
            "error": (
                "The full-stack dashboard pipeline is disabled "
                "(FULLSTACK_DASHBOARD_ENABLED=False)."
            ),
        }
    try:
        parsed = CreateFullstackDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid dashboard spec: {e}"}

    kb = _user_can_access_kb(
        db, user_id, org_id, project_id, parsed.datasource_id,
        resource_type=ctx.get("resource_type"),
        resource_id=ctx.get("resource_id"),
    )
    if kb is None:
        return {"success": False, "error": "Datasource not accessible in this project/org."}

    from app.models.dashboard_app import DashboardApp
    existing = db.query(DashboardApp).filter(DashboardApp.slug == parsed.slug).first()
    if existing:
        return {
            "success": False,
            "error": (
                f"A dashboard app with slug '{parsed.slug}' already exists. "
                "Use update_fullstack_dashboard or pick a different slug."
            ),
        }

    from app.services.dashboard_app.generator import get_generator
    from app.services.dashboard_app.manager import dashboard_app_manager

    spec = parsed.model_dump()
    # 1) Write the app files FIRST. T17: the global catch-all routes in
    #    routers/dashboards.py resolve the on-disk app dir per-request, so we no
    #    longer need a per-app mount. But we MUST verify the generator actually
    #    produced the files before marking the app "running" — otherwise the
    #    viewer's config.json fetch (and the whole app) is dead. Previously
    #    mount() masked this; now we fail loudly.
    get_generator().generate(spec)
    app_dir = get_generator().app_dir(parsed.slug)
    missing = [
        rel for rel in ("config.json", "api.py", str(Path("dist") / "index.html"))
        if not (app_dir / rel).exists()
    ]
    if missing:
        dashboard_app_manager.set_status(parsed.slug, "error")
        logger.error("create_fullstack_dashboard: generate incomplete for %s — missing %s",
                     parsed.slug, missing)
        return {"success": False,
                "error": "generate: missing " + ", ".join(missing)}

    # 2) T5: bind the chat thread that created this dashboard so My Files can
    #    offer "Open in chat" — the conversation id is the AgentConversation id
    #    the frontend deep-links via ?conv=<id>.
    chat_thread_id = (ctx.get("conversation_id") or "").strip() or None
    # T10: explicit scope wins; otherwise fall back to the config default so a
    #    user-created personal dashboard is never exposed org-wide by accident.
    from app.config import settings
    scope = parsed.scope or settings.DASHBOARD_DEFAULT_SCOPE
    record = dashboard_app_manager.create_app_record(
        spec, org_id=org_id, created_by_id=user_id, project_id=project_id,
        chat_thread_id=chat_thread_id, scope=scope,
    )
    try:
        dashboard_app_manager.mount(parsed.slug)
    except Exception as exc:
        logger.exception("create_fullstack_dashboard: deploy failed for %s", parsed.slug)
        dashboard_app_manager.set_status(parsed.slug, "error")
        return {"success": False, "error": f"Deploy failed: {exc}"}

    app_url = f"/api/dashboards/apps/{parsed.slug}/"
    dashboard_app_manager.set_status(parsed.slug, "running", app_url=app_url)
    # T6: first restore point — "undo" can bring the app back to its initial state.
    dashboard_app_manager.commit_version(parsed.slug, "create")
    # T18: persist a real Artifact row with source="dashboard_app" so the live
    #    dashboard shows up in My Files and passes the dashboard-turn guard
    #    (which drops every OTHER artifact written on a dashboard turn).
    try:
        from app.services.artifacts.artifact_service import ArtifactService
        svc = ArtifactService(db)
        svc.create_artifact(
            "dashboard", parsed.name,
            conversation_id=chat_thread_id,
            created_by_agent_id=user_id,
            org_id=org_id,
            source="dashboard_app",
            description=f"Live dashboard app: {app_url}",
        )
    except Exception:
        logger.exception("create_fullstack_dashboard: artifact persistence failed for %s",
                          parsed.slug)
    logger.info("full-stack dashboard created: %s (%d metrics)", parsed.slug, len(parsed.metrics))
    return {
        "success": True,
        "dashboard_app": {
            "slug": parsed.slug,
            "name": parsed.name,
            "status": "running",
            "app_url": app_url,
            "metric_count": len(parsed.metrics),
        },
        "artifact": {
            "source": "dashboard_app",
            "slug": parsed.slug,
            "title": parsed.name,
            "datasource_name": kb.name,
            "metric_count": len(parsed.metrics),
            "app_url": app_url,
            "chat_thread_id": chat_thread_id,
        },
    }


async def _update_fullstack_dashboard(
    args: dict, db, user_id, context: Optional[dict] = None,
) -> dict:
    """Regenerate + hot-reload an existing dashboard app (conversational iteration)."""
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    if not _fullstack_dashboard_enabled():
        return {
            "success": False,
            "error": (
                "The full-stack dashboard pipeline is disabled "
                "(FULLSTACK_DASHBOARD_ENABLED=False)."
            ),
        }
    try:
        parsed = UpdateFullstackDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid update spec: {e}"}

    from app.models.dashboard_app import DashboardApp
    record = (
        db.query(DashboardApp)
        .filter(DashboardApp.slug == parsed.slug, DashboardApp.org_id == org_id)
        .first()
    )
    if record is None:
        return {"success": False, "error": f"No dashboard app with slug '{parsed.slug}'."}

    current = dict(record.spec or {})
    if parsed.name is not None:
        current["name"] = parsed.name
    if parsed.description is not None:
        current["description"] = parsed.description
    if parsed.design_system_ref is not None:
        current["design_system_ref"] = parsed.design_system_ref
    if parsed.metrics is not None:
        current["metrics"] = [m.model_dump() for m in parsed.metrics]
    if parsed.refresh_interval_seconds is not None:
        current["refresh_interval_seconds"] = parsed.refresh_interval_seconds
    if parsed.theme is not None:
        current["theme"] = parsed.theme
    current["datasource_id"] = record.datasource_kb_id  # immutable after creation

    from app.services.dashboard_app.manager import dashboard_app_manager

    try:
        dashboard_app_manager.regenerate(parsed.slug, current)
    except Exception as exc:
        logger.exception("update_fullstack_dashboard: regenerate failed for %s", parsed.slug)
        dashboard_app_manager.set_status(parsed.slug, "error")
        return {"success": False, "error": f"Regenerate failed: {exc}"}

    record.spec = current
    record.status = "running"
    record.refresh_interval_seconds = int(current.get("refresh_interval_seconds", 30))
    # T5: keep the chat link fresh when iterating from a conversation (e.g. the
    # user re-opens an old dashboard in a new chat and edits it there).
    conv_id = (ctx.get("conversation_id") or "").strip() or None
    if conv_id and record.chat_thread_id != conv_id:
        record.chat_thread_id = conv_id
    db.commit()
    logger.info("full-stack dashboard updated: %s", parsed.slug)
    return {
        "success": True,
        "dashboard_app": {
            "slug": parsed.slug,
            "name": current.get("name", parsed.slug),
            "status": "running",
            "app_url": record.app_url or f"/api/dashboards/apps/{parsed.slug}/",
        },
    }


async def _list_fullstack_dashboards(
    args: dict, db, user_id, context: Optional[dict] = None,
) -> dict:
    """List dashboard apps the user can see (discoverability for the agent)."""
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    from app.services.dashboard_app.manager import dashboard_app_manager
    apps = dashboard_app_manager.list_apps(org_id=org_id)
    return {
        "success": True,
        "dashboards": [
            {
                "slug": a.slug,
                "name": a.name,
                "status": a.status,
                "app_url": a.app_url,
                "metric_count": len((a.spec or {}).get("metrics", [])),
            }
            for a in apps
        ],
    }


CREATE_FULLSTACK_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_fullstack_dashboard",
        "description": (
            "Build a full-stack real-time dashboard application: FastAPI "
            "sub-router + pre-built React frontend + WebSocket live data, "
            "generated from a DashboardSpec (Jinja2 templates). "
            "REQUIRED WORKFLOW: (1) call uiux_design_system(query=..., "
            "persist=True) first and pass the returned design_system_ref; "
            "(2) confirm the data contract with the user (which datasource, "
            "which metrics, read-only SQL) — NEVER fabricate data; "
            "(3) T10: if the user did not say, ASK whether this dashboard "
            "should be scope='personal' (only they can see it) or "
            "scope='company' (whole org can see it) — then submit this spec "
            "with real read-only SELECT queries. "
            "Responds with slug + app_url. Iterate later via "
            "update_fullstack_dashboard(slug=...)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Dashboard display name."},
                "slug": {
                    "type": "string",
                    "description": "URL-safe unique id: lowercase [a-z0-9_-], 1-64 chars.",
                },
                "description": {"type": "string", "description": "Optional."},
                "datasource_id": {
                    "type": "string",
                    "description": "KnowledgeBase id that the metrics query against.",
                },
                "design_system_ref": {
                    "type": "string",
                    "description": "Path returned by uiux_design_system(persist=True), e.g. design-system/{org}/MASTER.md.",
                },
                "metrics": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": sorted(FULLSTACK_ALLOWED_TYPES)},
                            "title": {"type": "string"},
                            "sql": {"type": "string", "description": "Read-only SELECT/WITH."},
                            "options": {"type": "object"},
                        },
                        "required": ["id", "type", "title", "sql"],
                    },
                },
                "refresh_interval_seconds": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 3600,
                    "default": 30,
                },
                "theme": {"type": "string", "enum": ["light", "dark"], "default": "light"},
                "scope": {
                    "type": "string",
                    "enum": ["personal", "company"],
                    "default": "personal",
                    "description": "personal = creator only; company = whole org. Ask the user if unspecified.",
                },
            },
            "required": ["name", "slug", "datasource_id", "metrics"],
        },
    },
}

UPDATE_FULLSTACK_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_fullstack_dashboard",
        "description": (
            "Hot-reload an existing full-stack dashboard app. Provide the slug "
            "from create_fullstack_dashboard plus any fields to change "
            "(metrics, theme, refresh interval, name, design_system_ref). "
            "Regenerates the app and restarts its realtime poller."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Existing app slug."},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "design_system_ref": {"type": "string"},
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": sorted(FULLSTACK_ALLOWED_TYPES)},
                            "title": {"type": "string"},
                            "sql": {"type": "string"},
                            "options": {"type": "object"},
                        },
                        "required": ["id", "type", "title", "sql"],
                    },
                },
                "refresh_interval_seconds": {"type": "integer", "minimum": 5, "maximum": 3600},
                "theme": {"type": "string", "enum": ["light", "dark"]},
            },
            "required": ["slug"],
        },
    },
}

LIST_FULLSTACK_DASHBOARDS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_fullstack_dashboards",
        "description": "List dashboard apps visible to the current org (slug, status, app_url).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

REVERT_FULLSTACK_DASHBOARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "revert_fullstack_dashboard",
        "description": (
            "Undo the last accepted change to a full-stack dashboard app. Restores "
            "the previous generated version (files + config) and hot-reloads the "
            "app. Use when the user is unhappy with the latest edit and asks to "
            "undo / revert / go back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Existing app slug."},
            },
            "required": ["slug"],
        },
    },
}


async def _revert_fullstack_dashboard(
    args: dict, db, user_id, context: Optional[dict] = None,
) -> dict:
    """Restore the most recent snapshot of a dashboard app (undo last change)."""
    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    if not _fullstack_dashboard_enabled():
        return {
            "success": False,
            "error": (
                "The full-stack dashboard pipeline is disabled "
                "(FULLSTACK_DASHBOARD_ENABLED=False)."
            ),
        }
    try:
        parsed = RevertFullstackDashboardArgs.model_validate(args)
    except ValidationError as e:
        return {"success": False, "error": f"Invalid args: {e}"}

    from app.models.dashboard_app import DashboardApp
    record = (
        db.query(DashboardApp)
        .filter(DashboardApp.slug == parsed.slug, DashboardApp.org_id == org_id)
        .first()
    )
    if record is None:
        return {"success": False, "error": f"No dashboard app with slug '{parsed.slug}'."}

    from app.services.dashboard_app.manager import dashboard_app_manager

    result = dashboard_app_manager.revert(parsed.slug)
    return {"success": result.get("success", False),
            "message": result.get("message"),
            "slug": parsed.slug}


try:
    from app.services.tool_registry import registry

    registry.register(
        name="create_fullstack_dashboard",
        schema=CREATE_FULLSTACK_DASHBOARD_SCHEMA,
        handler=_create_fullstack_dashboard,
        category="dashboards",
        toolset="dashboards",
        description="Build a full-stack real-time dashboard app from a DashboardSpec.",
        emoji="\U0001f4ca",
        is_async=True,
        check_fn=_fullstack_dashboard_enabled,
        max_result_size_chars=4_000,
    )
    registry.register(
        name="update_fullstack_dashboard",
        schema=UPDATE_FULLSTACK_DASHBOARD_SCHEMA,
        handler=_update_fullstack_dashboard,
        category="dashboards",
        toolset="dashboards",
        description="Hot-reload an existing full-stack dashboard app.",
        emoji="\U0001f504",
        is_async=True,
        check_fn=_fullstack_dashboard_enabled,
        max_result_size_chars=4_000,
    )
    registry.register(
        name="list_fullstack_dashboards",
        schema=LIST_FULLSTACK_DASHBOARDS_SCHEMA,
        handler=_list_fullstack_dashboards,
        category="dashboards",
        toolset="dashboards",
        description="List dashboard apps for the current org.",
        emoji="\U0001f5c2\ufe0f",
        is_async=True,
        check_fn=_fullstack_dashboard_enabled,
        max_result_size_chars=4_000,
    )
    registry.register(
        name="revert_fullstack_dashboard",
        schema=REVERT_FULLSTACK_DASHBOARD_SCHEMA,
        handler=_revert_fullstack_dashboard,
        category="dashboards",
        toolset="dashboards",
        description="Undo the last accepted change to a full-stack dashboard app.",
        emoji="\u21a9\ufe0f",
        is_async=True,
        check_fn=_fullstack_dashboard_enabled,
        max_result_size_chars=4_000,
    )
except Exception:
    logger.exception("Failed to register full-stack dashboard tools")
