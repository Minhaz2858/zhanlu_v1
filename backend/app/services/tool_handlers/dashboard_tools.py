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


def _stamp_dashboard_conversation(db, conv_id: str | None, *, dashboard_id: str | None = None,
                                  dashboard_slug: str | None = None,
                                  dashboard_name: str | None = None) -> None:
    """Mark a chat session as DASHBOARD-DEDICATED (dashboard mode).

    Writes ``metadata_.mode = 'dashboard'`` plus the bound dashboard's
    id/slug/name onto the AgentConversation so:
      - the chat header can show a "Dashboard" badge (project / dashboard /
        model), telling the user AND the agent this session is dedicated to
        building/editing one dashboard;
      - the sidebar can render a distinct dashboard icon for the session;
      - reopening the session later (``?conv=<id>``) restores full context —
        the agent sees its own dashboard build history and knows which app
        to ``update_fullstack_dashboard``.

    No-op when ``conv_id`` is missing or the conversation doesn't exist.
    """
    if not conv_id:
        return
    try:
        from app.models.agent_conversation import AgentConversation
        conv = db.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
        if conv is None:
            return
        meta = dict(conv.metadata_ or {})
        meta["mode"] = "dashboard"
        if dashboard_id:
            meta["dashboard_id"] = dashboard_id
        if dashboard_slug:
            meta["dashboard_slug"] = dashboard_slug
        if dashboard_name:
            meta["dashboard_name"] = dashboard_name
        conv.metadata_ = meta
        # Legacy binding column (FK to dashboards.id) kept in sync for
        # existing /from-conv consumers; fullstack apps use metadata.slug.
        if dashboard_id:
            conv.dashboard_id = dashboard_id
        db.commit()
    except Exception:
        logger.exception("stamp dashboard conversation failed (conv=%s)", conv_id)

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
    # Bind this dashboard to the active conversation (1:1) AND mark the
    # session as dashboard-dedicated so the header/sidebar show the badge and
    # reopening the session restores full build/edit context. If the
    # conversation already had a bound dashboard, the new one replaces the
    # binding — the old dashboard is NOT deleted (still findable in MySpace).
    conv_id = ctx.get("conversation_id")
    if conv_id:
        _stamp_dashboard_conversation(
            db, conv_id,
            dashboard_id=d.id,
            dashboard_name=d.name,
        )
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
    "kpi", "line", "bar", "pie", "table", "area", "gauge", "radar", "combo",
    # Compact dual-pane trend card (12-month + 30-day style mini charts):
    # renders a small line chart with optional action pill + confidence badge
    # (the Ecisco CEO "product signal card" look). Data = label + value cols.
    "sparkline",
}

# Typed AI-analysis panels (the decision-center information architecture).
# Each panel is AI-authored from REAL queried data (the agent computes the
# figures via execute_query / metric deltas, then narrates — never invents).
# See CREATE_FULLSTACK_DASHBOARD_SCHEMA for the full per-type field docs.
ALLOWED_PANEL_TYPES = {
    "alerts", "decisions", "narrative", "chain",
    "customers", "inventory", "competitors", "news",
}

ALLOWED_SEVERITIES = {"crit", "warn", "opp", "info"}
ALLOWED_TONES = {"up", "down", "neutral", "warn", "good", "bad"}


def _validate_panels(panels: list) -> None:
    """Lightweight structural validation for typed analysis panels.

    Kept permissive (extra fields ignored) so the LLM can narrate rich
    analysis without fighting a rigid schema — but the `type` must be a
    known panel type and required per-type fields must be present, so a
    malformed spec fails fast instead of rendering an empty card.
    """
    for p in panels:
        ptype = (p or {}).get("type")
        if ptype not in ALLOWED_PANEL_TYPES:
            raise ValueError(
                f"panels[].type must be one of {sorted(ALLOWED_PANEL_TYPES)}, got {ptype!r}"
            )
        items = p.get("items") or p.get("rows") or []
        if ptype in ("alerts", "decisions", "customers", "inventory", "competitors", "news"):
            if not isinstance(items, list) or not items:
                raise ValueError(f"panel type '{ptype}' requires a non-empty items/rows list")
        if ptype == "chain":
            nodes = p.get("nodes")
            if not isinstance(nodes, list) or not nodes:
                raise ValueError("panel type 'chain' requires a non-empty nodes list")
        if ptype == "narrative":
            if not p.get("body"):
                raise ValueError("panel type 'narrative' requires a body")


def _validate_pages(pages: list) -> None:
    for pg in pages:
        if not (pg or {}).get("id") or not (pg or {}).get("label"):
            raise ValueError("pages[] entries require both 'id' and 'label'")

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
    # Visual style variant. "standard" = light/dark BI; "chinese_bi" = 大屏
    # Chinese BI style (dark navy glow, red-up/green-down semantics, Chinese
    # font stack, animated numbers) for China-region customers.
    # "ceo" = dark petroleum decision center (near-black ink, amber/gold
    # accent, alert-strip insights) — the Ecisco CEO Command Center look.
    # "editorial" = light print report (warm paper, Fraunces/Newsreader serif
    # + IBM Plex Mono numerals, maroon accent) — the EDIA Sales Report look.
    style: str = "standard"
    # T10: "personal" (creator only) or "company" (whole org). The agent
    # should ask the user at creation time; falls back to the config default.
    scope: str = "personal"
    # AI insight strip: 1-3 {"title", "body"} pairs rendered above the grid.
    insights: list[dict] = Field(default_factory=list)
    # Optional sectioned layout: [{"title": str, "widgets": [metric_id, ...]}].
    # Groups widgets into titled sections (KPI strip → trends → breakdown) so
    # the dashboard tells a story instead of one flat grid. Metrics not listed
    # fall into a trailing "Other" section at render time.
    # Sections may also carry:
    #   "page": str       — which tab (pages[].id) the section belongs to
    #   "panels": [id]    — panel ids rendered in a right-hand rail (2-column
    #                       split on wide screens; left = widgets, right = panels)
    #   "columns": 2      — force the 2-column split (default when panels given)
    layout: list[dict] = Field(default_factory=list)
    # Multi-page tabs (the Ecisco CEO decision center has 5 pages). Each entry
    # {"id": "overview", "label": "CEO 总览"}. When empty, a single "Overview"
    # page is rendered (backward compatible). Panels and layout sections can
    # target a page via their "page" key; anything without a page lands on the
    # first page.
    pages: list[dict] = Field(default_factory=list)
    # Typed AI-analysis panels — the decision-center information architecture.
    # Each panel: {"id", "type", "page"?, "span"? ("full"|"half"|"third"),
    #              ...type-specific fields}. The agent narrates these from REAL
    # queried data (execute_query results / computed deltas) — never fabricate
    # figures. Supported types:
    #   alerts     {"items": [{severity: crit|warn|opp|info, icon, title,
    #              body (data → why → recommendation), cta, time}]}
    #   decisions  {"items": [{tag, tag_tone, title, action, action_tone,
    #              body, pnl, pnl_tone, buttons: [..]}]}
    #   narrative  {"title", "body"}  — long-form AI analysis block (AI badge)
    #   chain      {"title", "nodes": [{label, value, unit, delta, delta_tone,
    #              note, note_tone}]} — upstream cost / value cascade
    #   customers  {"rows": [{avatar, name, sub, revenue, status, status_tone}]}
    #   inventory  {"rows": [{label, weeks, max, tone}]} — coverage bars
    #   competitors{"rows": [{name, our_price, lo, hi,
    #              comps: [{name, price}], diff, diff_tone}]} — position bands
    #   news       {"rows": [{time, badge, badge_tone, text}]} — activity feed
    panels: list[dict] = Field(default_factory=list)
    # Page header extras (executive dashboard): {"greeting": str,
    #   "snapshot": [{label, value, delta, delta_tone}], "period": str}.
    # Snapshot chips render like "布伦特 $79.4 ↓$1.2 · 石脑油 $642 ↓$9".
    header: Optional[dict] = None
    # Page footer (data provenance): {"sources": str} — e.g. "数据来源：ERP + 市场数据".
    footer: Optional[dict] = None

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

    @field_validator("style")
    def _style_ok(cls, v: str) -> str:
        if v not in ("standard", "chinese_bi", "ceo", "editorial"):
            raise ValueError(
                "style must be 'standard', 'chinese_bi', 'ceo' or 'editorial'"
            )
        return v

    @field_validator("scope")
    def _scope_ok(cls, v: str) -> str:
        if v not in ("personal", "company"):
            raise ValueError("scope must be 'personal' or 'company'")
        return v

    @field_validator("pages")
    @classmethod
    def _pages_ok(cls, v: list) -> list:
        _validate_pages(v)
        return v

    @field_validator("panels")
    @classmethod
    def _panels_ok(cls, v: list) -> list:
        _validate_panels(v)
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
    style: Optional[str] = None
    insights: Optional[list[dict]] = None
    layout: Optional[list[dict]] = None
    pages: Optional[list[dict]] = None
    panels: Optional[list[dict]] = None
    header: Optional[dict] = None
    footer: Optional[dict] = None

    @field_validator("slug")
    def _slug_ok(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("slug must be lowercase [a-z0-9_-], 1-64 chars")
        return v

    @field_validator("pages")
    @classmethod
    def _pages_ok(cls, v: Optional[list]) -> Optional[list]:
        if v is not None:
            _validate_pages(v)
        return v

    @field_validator("panels")
    @classmethod
    def _panels_ok(cls, v: Optional[list]) -> Optional[list]:
        if v is not None:
            _validate_panels(v)
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


# ── Professional quality gate (2026-08-28) ──────────────────────────────
# After a dashboard is generated, compute a deterministic quality report so
# the agent gets instant feedback and can iterate via update_fullstack_
# dashboard instead of shipping a thin/one-chart board. Mechanical counts —
# "Python computes, LLM narrates": the agent reads `quality.recommendations`
# and acts on them, it never invents these figures.

_TREND_TYPES = {"line", "area", "combo"}
_BREAKDOWN_TYPES = {"bar", "pie", "table", "radar", "gauge"}


def _dashboard_quality_report(spec: dict) -> dict:
    """Score a DashboardSpec against the professional-visual standard.

    Returns a dict with mechanical widget-mix counts + a passing verdict +
    concrete recommendations (what to change to reach the standard). The
    create/update tools embed this in their result so the agent can
    self-correct in the same turn.
    """
    metrics = spec.get("metrics") or []
    layout = spec.get("layout") or []
    insights = spec.get("insights") or []

    kpi_count = sum(1 for m in metrics if (m.get("type") or "") == "kpi")
    trend_count = sum(1 for m in metrics if (m.get("type") or "") in _TREND_TYPES)
    breakdown_count = sum(1 for m in metrics if (m.get("type") or "") in _BREAKDOWN_TYPES)
    filter_count = sum(
        1 for m in metrics if (m.get("options") or {}).get("filters")
    )

    recommendations: list[str] = []
    hard_gaps: list[str] = []
    if not metrics:
        hard_gaps.append("widget_count")
        recommendations.append(
            "No metrics defined — a dashboard needs at least 3 widgets."
        )
    else:
        if len(metrics) < 5:
            hard_gaps.append("widget_count")
            recommendations.append(
                f"Only {len(metrics)} widget(s); reach 5+ by adding angles: "
                "KPI row, time trend, comparison bar, composition, detail table."
            )
        if kpi_count < 2:
            hard_gaps.append("kpi_count")
            recommendations.append(
                "Add at least 2 KPI cards (headline numbers with options.delta "
                "for the vs-prev-period change)."
            )
        if trend_count < 1:
            hard_gaps.append("trend_count")
            recommendations.append(
                "Add a time trend (line/area/combo) over the data's date column "
                "— a dashboard with no trend is not a dashboard."
            )
        if breakdown_count < 1:
            hard_gaps.append("breakdown_count")
            recommendations.append(
                "Add a breakdown angle (bar for comparison, pie/donut for "
                "composition, table for detail)."
            )
        if len(layout) < 2:
            hard_gaps.append("sections")
            recommendations.append(
                "Structure the layout into at least 2 sections (KPI cards first, "
                "then trends, then breakdowns/tables) — a single flat grid is "
                "not a professional board."
            )
        if filter_count == 0:
            hard_gaps.append("filters")
            recommendations.append(
                "Declare cross-widget filters (options.filters on the metric SQL "
                "with the BARE :dim_<key> token) so users can slice by "
                "product/category/date — every professional board has filters."
            )
        if len(metrics) > 8:
            recommendations.append(
                f"{len(metrics)} widgets is crowded; consolidate to ≤8 and merge "
                "redundant charts so every widget is a distinct analytical angle."
            )
    if not insights:
        hard_gaps.append("insights")
        recommendations.append(
            "Add an insights strip (1-3 titled bullets) narrating the real "
            "numbers — never fabricate figures."
        )
    if not spec.get("design_system_ref"):
        hard_gaps.append("design_tokens")
        recommendations.append(
            "No design_system_ref — run uiux_design_system(persist=True) first "
            "so the board wears the project's palette and typography."
        )

    passing = not hard_gaps
    return {
        "passing": passing,
        "grade": "A" if not recommendations else ("B" if passing else "C"),
        "hard_gaps": hard_gaps,
        "widget_count": len(metrics),
        "kpi_count": kpi_count,
        "trend_count": trend_count,
        "breakdown_count": breakdown_count,
        "section_count": len(layout) or 1,
        "insights_count": len(insights),
        "filter_count": filter_count,
        "design_system_applied": bool(spec.get("design_system_ref")),
        "recommendations": recommendations,
    }


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
    # Slug-collision auto-recovery (2026-08-28): the LLM frequently reuses a
    # slug from an earlier build ("sales-performance-dashboard-v2" twice), and
    # failing hard made the whole dashboard turn die with "Sorry, I hit an
    # error while responding" (conv 5f2c2c39). Instead of erroring, determin-
    # istically uniquify: keep the requested slug if free, else append a
    # numeric suffix. The dashboard NAME is unchanged; only the URL slug moves.
    _final_slug = parsed.slug
    if db.query(DashboardApp).filter(DashboardApp.slug == _final_slug).first():
        _suffix = 2
        while db.query(DashboardApp).filter(
            DashboardApp.slug == f"{_final_slug}-{_suffix}"
        ).first():
            _suffix += 1
        _final_slug = f"{_final_slug}-{_suffix}"
        logger.warning(
            "create_fullstack_dashboard: slug '%s' taken; auto-uniquified to '%s'",
            parsed.slug, _final_slug,
        )
    parsed.slug = _final_slug

    from app.services.dashboard_app.generator import get_generator
    from app.services.dashboard_app.manager import dashboard_app_manager

    spec = parsed.model_dump()
    # 0) Lock server-side analytics BEFORE generating: run every metric's
    #    own SQL through the shared QueryService path and attach computed
    #    deltas (KPI options.delta) + top items + deterministic insight
    #    bullets. "Python computes, LLM narrates" — the LLM never invents
    #    these figures (see analytics.py docstring / Aug 2026 failure).
    try:
        from app.services.dashboard_app.analytics import enrich_spec
        spec = enrich_spec(db, kb.id, spec)
    except Exception as exc:  # noqa: BLE001 — analytics is best-effort
        logger.warning("create_fullstack_dashboard: analytics enrichment failed (non-fatal): %s", exc)
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
    # Mark the originating chat session as DASHBOARD-DEDICATED. The header
    # badge, the sidebar icon and the reopen-with-context flow all key off
    # conversation metadata.mode == 'dashboard' — without this stamp the
    # session looks like a generic chat and the agent can forget it owns a
    # dashboard (the Aug 2026 "delivered a text report instead" failure).
    _stamp_dashboard_conversation(
        db, chat_thread_id,
        dashboard_slug=parsed.slug,
        dashboard_name=parsed.name,
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
    # Professional quality gate (2026-08-28): score the generated spec and
    # surface the verdict to the agent so it can self-correct the same turn
    # (e.g. add a trend/insights/filters) via update_fullstack_dashboard.
    _quality = _dashboard_quality_report(spec)
    return {
        "success": True,
        "dashboard_app": {
            "slug": parsed.slug,
            "name": parsed.name,
            "status": "running",
            "app_url": app_url,
            "metric_count": len(parsed.metrics),
        },
        "quality": _quality,
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
        # Lock server-side analytics for the CHANGED metrics before
        # regenerating — recompute deltas / top items from live data so the
        # edited dashboard shows fresh, non-fabricated figures.
        try:
            from app.services.dashboard_app.analytics import enrich_spec
            current = enrich_spec(db, record.datasource_kb_id, current)
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_fullstack_dashboard: analytics enrichment failed (non-fatal): %s", exc)
    if parsed.refresh_interval_seconds is not None:
        current["refresh_interval_seconds"] = parsed.refresh_interval_seconds
    if parsed.theme is not None:
        current["theme"] = parsed.theme
    if parsed.style is not None:
        current["style"] = parsed.style
    if parsed.insights is not None:
        current["insights"] = parsed.insights
    if parsed.layout is not None:
        current["layout"] = parsed.layout
    if parsed.pages is not None:
        current["pages"] = parsed.pages
    if parsed.panels is not None:
        current["panels"] = parsed.panels
    if parsed.header is not None:
        current["header"] = parsed.header
    if parsed.footer is not None:
        current["footer"] = parsed.footer
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
    # Stamp the editing conversation as dashboard-dedicated too — so the
    # sidebar shows the dashboard icon and the header badge on this session.
    _stamp_dashboard_conversation(
        db, conv_id,
        dashboard_slug=parsed.slug,
        dashboard_name=current.get("name") or parsed.slug,
    )
    logger.info("full-stack dashboard updated: %s", parsed.slug)
    # Professional quality gate (2026-08-28): same verdict as create so an
    # edit that regresses the widget mix (e.g. a layout-only update that
    # dropped the trend) is flagged back to the agent immediately.
    _quality = _dashboard_quality_report(current)
    return {
        "success": True,
        "dashboard_app": {
            "slug": parsed.slug,
            "name": current.get("name", parsed.slug),
            "status": "running",
            "app_url": record.app_url or f"/api/dashboards/apps/{parsed.slug}/",
        },
        "quality": _quality,
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
                "style": {
                    "type": "string",
                    "enum": ["standard", "chinese_bi", "ceo", "editorial"],
                    "default": "standard",
                    "description": (
                        "Visual style variant. standard = clean light/dark BI. "
                        "chinese_bi = 大屏 China BI (dark navy glow, red=up/green=down) "
                        "for China-region customers. ceo = dark petroleum decision "
                        "center (near-black ink + amber/gold accent, alert-strip "
                        "insights) — use for executive/CEO dashboards with KPI pulse "
                        "cards and decision summaries. editorial = light print report "
                        "(warm paper, serif display + mono numerals, maroon accent) — "
                        "use for polished sales/ops reports. Add 1-3 insights (title+"
                        "body) whenever style=ceo or editorial — they render as "
                        "severity-rail alert cards / pull quotes."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": ["personal", "company"],
                    "default": "personal",
                    "description": "personal = creator only; company = whole org. Ask the user if unspecified.",
                },
                "insights": {
                    "type": "array",
                    "description": "AI insight strip: 1-3 {title, body} pairs summarizing what the numbers mean (Python-computed, LLM-narrated). Never fabricate figures.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                    },
                },
                "layout": {
                    "type": "array",
                    "description": "Sectioned layout: [{\"title\", \"widgets\": [metric_id,...], \"page\"?, \"panels\"?: [panel_id,...]}]. Sections tell a story (KPI strip → trends → breakdown). A section with \"panels\" renders a 2-column split (left = widgets, right = panels rail). Sections with \"page\" land on that tab (pages[].id).",
                    "items": {"type": "object"},
                },
                "pages": {
                    "type": "array",
                    "description": "Multi-page tabs — the Ecisco CEO decision center has 5 pages. Each {\"id\", \"label\"} (e.g. {\"id\": \"overview\", \"label\": \"CEO 总览\"}). Panels and layout sections target a page via their \"page\" key; entries without a page land on the first tab. Omit for a single-page dashboard.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                        },
                    },
                },
                "panels": {
                    "type": "array",
                    "description": "Typed AI-analysis panels — the decision-center information architecture (what makes an executive dashboard professional). Each panel: {\"id\", \"type\", \"page\"?, \"span\"? (full|half|third), ...fields}. NARRATE FROM REAL QUERIED DATA — compute figures via execute_query / metric deltas, NEVER invent numbers. Types: alerts {\"items\": [{severity: crit|warn|opp|info, icon, title, body, cta, time}]} — each alert body = data → why it matters → recommended action; decisions {\"items\": [{tag, tag_tone, title, action, action_tone, body, pnl, pnl_tone, buttons}]} — approval cards with quantified P&L impact badge; narrative {\"title\", \"body\"} — long-form AI analysis block; chain {\"title\", \"nodes\": [{label, value, unit, delta, delta_tone, note, note_tone}]} — cost/value cascade (e.g. Brent → Naphtha → cost baseline); customers {\"rows\": [{avatar, name, sub, revenue, status, status_tone}]} — account health; inventory {\"rows\": [{label, weeks, max, tone}]} — coverage bars; competitors {\"rows\": [{name, our_price, lo, hi, comps: [{name, price}], diff, diff_tone}]} — pricing position bands; news {\"rows\": [{time, badge, badge_tone, text}]} — activity feed.",
                    "items": {"type": "object"},
                },
                "header": {
                    "type": "object",
                    "description": "Executive page header extras: {\"greeting\": \"早上好，刘总 — 今日有 3 项决策等待批准\", \"snapshot\": [{\"label\": \"布伦特\", \"value\": \"$79.4\", \"delta\": \"↓$1.2\", \"delta_tone\": \"down\"}], \"period\": \"W-2025-23\"}.",
                },
                "footer": {
                    "type": "object",
                    "description": "Page footer (data provenance): {\"sources\": \"数据来源：ERP + 市场数据\"}.",
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
                "insights": {
                    "type": "array",
                    "description": "AI insight strip: 1-3 {title, body} pairs. Omit to keep current insights.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                    },
                },
                "layout": {
                    "type": "array",
                    "description": "Sectioned layout (see create_fullstack_dashboard). Omit to keep current layout.",
                    "items": {"type": "object"},
                },
                "pages": {
                    "type": "array",
                    "description": "Multi-page tabs [{id, label}]. Omit to keep current pages.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                        },
                    },
                },
                "panels": {
                    "type": "array",
                    "description": "Typed AI-analysis panels (see create_fullstack_dashboard). Omit to keep current panels.",
                    "items": {"type": "object"},
                },
                "header": {"type": "object", "description": "Executive header extras {greeting, snapshot, period}."},
                "footer": {"type": "object", "description": "Page footer {sources}."},
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
