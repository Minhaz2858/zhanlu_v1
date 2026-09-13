"""build_session_state_block — inject session state into the LLM system prompt."""
from __future__ import annotations
from typing import Optional

from sqlalchemy.orm import Session

from app.services.data_execution.session_state import SessionStateService


def build_session_state_block(db: Session, *, session_id: str) -> Optional[str]:
    state = SessionStateService.get_last(db, session_id=session_id)
    if not state or not state.last_execution_id:
        return None
    return (
        "\n\nSESSION STATE — last data tool execution in this conversation:\n"
        f"  execution_id: {state.last_execution_id}\n"
        f"  tool_name: {state.last_tool_name}\n"
        f"  data_signature: {state.last_data_signature or ''}\n"
        "  When the user asks to re-export or re-format a previous analysis,\n"
        "  pass this execution_id to create_artifact(source_execution_id=...)\n"
        "  instead of re-running data tools."
    )


def build_dashboard_state_block(db: Session, *, conversation_id: str) -> Optional[str]:
    """Inject the dashboard this conversation owns (ONE dashboard per session).

    Root cause of the Sep-2026 duplicate-dashboard bug: follow-up requests
    ("make it white and blue", "widgets missing", "rebuild it") never mention
    the word dashboard or its slug, so the LLM had no reliable way to know the
    conversation already owns a board — it re-ran create_fullstack_dashboard,
    and the slug auto-uniquify silently spawned -2/-5/-5-2 clones. This block
    names the owned dashboard (anchor: dashboard_apps.chat_thread_id, which
    survives conversation-row churn) and forces the update tool.

    Returns None when the conversation owns no dashboard (fail-open).
    """
    if not conversation_id:
        return None
    try:
        from app.models.dashboard_app import DashboardApp

        owned_rows = (
            db.query(DashboardApp)
            .filter(DashboardApp.chat_thread_id == conversation_id)
            .order_by(DashboardApp.created_date.desc())
            .all()
        )
        owned_rows = [o for o in owned_rows if not getattr(o, "is_deleted", False)]
        if not owned_rows:
            return None
        lines = []
        for owned in owned_rows:
            app_url = getattr(owned, "app_url", None) or (
                f"/api/dashboards/apps/{owned.slug}/"
            )
            lines.append(
                f"  - '{owned.name}' slug={owned.slug} app_url={app_url}\n"
            )
        return (
            "\n\nDASHBOARD STATE — dashboard(s) owned by this conversation "
            "(ONE per chat session by design):\n"
            + "".join(lines)
            + "  If the user asks to update / restyle / re-theme / re-layout /\n"
            "  rebuild one of these dashboards or fix its data/widgets, call\n"
            "  update_fullstack_dashboard(slug='<matching slug>', ...) — the\n"
            "  app regenerates and hot-reloads in place.\n"
            "  NEVER call create_fullstack_dashboard for a dashboard this\n"
            "  conversation already owns — it will refuse and redirect you\n"
            "  here. If the user describes a dashboard that is NOT listed,\n"
            "  use list_fullstack_dashboards to find its slug first."
        )
    except Exception:  # noqa: BLE001 — fail-open, never crash the turn
        return None
