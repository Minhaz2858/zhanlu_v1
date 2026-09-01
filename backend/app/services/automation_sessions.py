"""Automation session service — origin-session binding + project lock.

Extracted from ``routers/automation_api.py`` so the executor can call it
without importing from the router layer (which would invert the
dependency). Lives in the services layer alongside the executor and
dispatcher.

Origin-session binding (2026-08-12): run output lives in the SAME
origin project chat session where the task was created — NO dedicated
per-task chat, NO separate ?conv= URL.  This overrides the earlier
"Manus-style one-chat-per-task" design.

Two responsibilities beyond the original helper:

1. **Project-resolved tagging.** New sessions and conversations are
   tagged from the task's RESOLVED project identity
   (``_resolve_task_project``), which adopts+persists the FK when only
   the legacy name is present — so the session is born under the right
   project even for legacy tasks.

2. **Drift reconciliation.** An existing session whose ``project_id`` /
   ``project`` no longer matches the task's resolved project is
   corrected IN PLACE (not re-adopted) on the next ensure call. This
   fixes sessions that drifted before the FK was adopted, or that were
   tagged with a sibling project.
"""
from __future__ import annotations

import logging
from datetime import datetime as _dt
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.models.agent_conversation import AgentConversation
from app.models.automation_task import AutomationTask
from app.models.chat_session import ChatSession

logger = logging.getLogger(__name__)


# Placeholder session titles that trigger adoption of a fresh
# task-named ChatSession (linked to the existing conversation so past
# run history stays visible).
_PLACEHOLDER_TITLES = {
    "", "test", "new task", "new chat", "untitled", "untitled chat",
    "new automation", "automation",
}


def _resolved_session_project(
    db: Session, task: AutomationTask
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(project_id, project_name)`` for tagging a session, using
    the task's RESOLVED project identity.

    Calls ``_resolve_task_project`` which (since the project-lock change)
    persists the adopted FK when only the legacy name is present, so the
    task's ``project_id`` is the frozen identity afterwards. The name
    comes from the Project row when the FK is known, else the legacy name
    or ``"global"`` — preserving the backend's existing convention that
    sessions carry a non-null ``project`` string.
    """
    # Local import to avoid a module-load cycle (executor imports are
    # heavy and pull in many service deps).
    from app.services.automation_executor import _resolve_task_project
    from app.models.project import Project

    project_id, project_name = _resolve_task_project(db, task)
    if project_id:
        proj = db.get(Project, project_id)
        name = getattr(proj, "name", None) if proj else project_name
        return project_id, (name or "global")
    # Workspace-global task: no FK, no legacy name → "global".
    return None, (project_name or getattr(task, "project", None) or "global")


def _session_title_matches(chat: ChatSession, task_name: str) -> bool:
    """True iff the session's title is already the task's name (happy path)."""
    cur = (chat.title or "").strip()
    if cur.lower() == task_name.lower():
        return True
    if cur.lower() in _PLACEHOLDER_TITLES or cur.startswith("new "):
        return False
    return False  # non-matching, non-placeholder title still counts as "shared"


def _reconcile_session_project(
    db: Session,
    chat: ChatSession,
    resolved_project_id: Optional[str],
    resolved_project_name: str,
) -> bool:
    """Correct a session whose project tags drifted from the resolved
    identity. Returns True iff a write was made. Non-matching is NOT an
    error — the session is fixed in place and reused."""
    changed = False
    if chat.project_id != resolved_project_id:
        chat.project_id = resolved_project_id
        changed = True
    if (chat.project or "global") != resolved_project_name:
        chat.project = resolved_project_name
        changed = True
    if changed:
        db.commit()
        logger.info(
            "ensure_task_chat_session: reconciled session %s project -> "
            "id=%s name=%s",
            chat.id, resolved_project_id, resolved_project_name,
        )
    return changed


def ensure_task_chat_session(
    db: Session, task: AutomationTask
) -> Tuple[str, bool]:
    """Return ``(session_id, created)`` for the task's chat session.

    Origin-session binding (2026-08-12): run output lives in the SAME
    origin project chat session where the task was created — NO dedicated
    per-task chat, NO separate ?conv= URL.  This overrides the earlier
    "Manus-style one-chat-per-task" design.

    1. Resolve the task's project identity ONCE (adopts+persists the FK
       when only the legacy name is present).
    2. If the task has a ``session_id`` and the session exists → reconcile
       the session's project tags in place if they drifted, then return it.
       This works for both origin sessions and any legacy dedicated sessions.
    3. If the task has no ``session_id`` or the session was deleted → create
       a new ChatSession + AgentConversation pair tagged from the resolved
       project, link the task to it.

    Returns ``(session_id, created)`` where ``created`` is True iff a
    new ChatSession was allocated.
    """
    task_name = (task.name or "Untitled Automation").strip()
    resolved_project_id, resolved_project_name = _resolved_session_project(db, task)

    # Re-read the task's now-persisted project_id (the resolver may have
    # written it) so any new session/conversation is tagged consistently.
    task_project_id = task.project_id or resolved_project_id

    # Origin-session binding (2026-08-12): use the task's existing session
    # (the one where the task was created) instead of creating a dedicated
    # automation session.  Run output goes to the same chat the user is
    # having the conversation in.
    if task.session_id:
        existing = db.query(ChatSession).filter(
            ChatSession.id == task.session_id,
            ChatSession.is_deleted == False,  # noqa: E712
        ).first()
        if existing is not None:
            # Reconcile project tags and return the existing session.
            _reconcile_session_project(
                db, existing, resolved_project_id, resolved_project_name,
            )
            if not existing.created_by_id and task.created_by_id:
                existing.created_by_id = task.created_by_id
                db.commit()
                logger.info(
                    "ensure_task_chat_session: backfilled created_by_id=%s on "
                    "session %s",
                    task.created_by_id, existing.id,
                )
            return existing.id, False
        # Session was deleted — fall through to create a new one.

    # ---- Create a new session (fallback for deleted or missing sessions) ----
    conv = AgentConversation(
        agent_name=None,
        title=task_name,
        messages=[],
        status="active",
        created_by_id=getattr(task, "created_by_id", None),
        project_id=task_project_id,
    )
    conv.metadata_ = {
        "source": "ensure_session",
        "task_id": task.id,
    }
    db.add(conv)
    db.flush()

    chat = ChatSession(
        title=task_name,
        project_id=resolved_project_id,
        project=resolved_project_name,
        conversation_id=conv.id,
        agent_name="automation_agent",
        starred=False,
        created_by_id=getattr(task, "created_by_id", None),
        last_message_at=_dt.utcnow().isoformat(),
    )
    db.add(chat)
    db.flush()
    old_sid = task.session_id
    # Merge task into this session before modifying (task may be detached
    # if the caller obtained it from a different DB session).
    _task = db.merge(task)
    _task.session_id = chat.id
    db.commit()
    logger.info(
        "ensure_task_chat_session: %s session %s (conv %s) "
        "for task %s (%s) project=%s%s",
        "replaced deleted" if old_sid else "created",
        chat.id, conv.id, task.id, task_name,
        resolved_project_name,
        f" (was {old_sid})" if old_sid else "",
    )
    return chat.id, True
