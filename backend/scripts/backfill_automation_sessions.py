"""
One-shot script: adopt a dedicated chat session for every automation
task that doesn't already have one (Manus-style one-task-one-session).

Background
----------
The original automation pipeline stored ``task.session_id`` only when
the task was created *from inside a chat* — in which case the chat
session was the one the user was typing in. Tasks created elsewhere
(``My Space``, scheduled imports, the agent builder dialog) kept
``session_id = None``, and the dispatcher's ``_notify_chat`` would
fall back to the user's most-recent chat session — almost always the
wrong conversation.

The fix going forward is in ``backend/app/services/agent_tools.py``
(auto-adopt on task creation) and the new
``POST /api/automations/{id}/ensure-session`` endpoint
(``backend/app/routers/automation_api.py``) which is called from the
frontend ``runNow`` before firing a run. Together those handle every
*new* task and every task the user manually re-runs.

This script handles the catch-up case: walk every existing task, and
if its current session title is a generic placeholder (``"test"``,
``"new task"``, etc.) or otherwise doesn't match the task's current
name, adopt a fresh ChatSession + AgentConversation pair and re-link
the task. The old session is left in place (other tasks may still
use it; we're a backfill, not a cleanup).

History preservation
--------------------
Each new ChatSession is paired with a *fresh* AgentConversation —
not the old one. Reasoning: the old conversation likely carries
messages from unrelated tasks or free-form chat, and the Manus-style
UX requires the per-task chat to show only that task's run history.
A future enhancement could re-link the new session to the old
conversation if a per-task run-message tag is added, but that's out
of scope for the backfill.

Database
--------
Uses SQLAlchemy + the backend's existing models so the same script
works on both SQLite (local dev) and Postgres (production). Set
``DATABASE_URL`` or use ``--url`` to override.

Usage
-----

::

    cd /root/zhanlu/backend
    PYTHONPATH=. python scripts/backfill_automation_sessions.py --dry-run
    PYTHONPATH=. python scripts/backfill_automation_sessions.py

    # Override the DB URL for a one-off run:
    PYTHONPATH=. python scripts/backfill_automation_sessions.py \\
        --url postgresql+psycopg2://user:pass@host:5432/dbname

The script is idempotent — running it again after a successful apply
will report "0 tasks adopted" because the broken sessions are gone.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime as _dt


PLACEHOLDER_TITLES = {
    "", "test", "new task", "new chat", "untitled", "untitled chat",
    "new automation", "automation",
}


def _session_title_matches(cur_title: str, task_name: str) -> bool:
    cur = (cur_title or "").strip()
    if cur.lower() == task_name.lower():
        return True
    if cur.lower() in PLACEHOLDER_TITLES or cur.startswith("new "):
        return False
    # A non-placeholder, non-matching title means the session is
    # shared with another task — we treat that as "needs adoption"
    # too, since per-task ownership is the whole point of this fix.
    return False


def adopt_session_for_task(db, task) -> tuple[str, bool]:
    """Adopt (or no-op) a dedicated chat session for ``task``.

    Mirrors ``_ensure_task_chat_session`` in ``automation_api.py`` —
    kept in sync so the script can run independently of the FastAPI
    app. The new ChatSession is paired with the task's existing
    AgentConversation when one is available, so past run results
    (which were written there by ``_notify_chat``) remain visible
    in the per-task chat. Falls back to a fresh AgentConversation
    only when the task has no prior conversation at all.

    Returns ``(session_id, created)``.
    """
    from app.models.agent_conversation import AgentConversation
    from app.models.chat_session import ChatSession

    task_name = (task.name or "Untitled Automation").strip()
    old_conv_id: str | None = None
    if task.session_id:
        existing = db.query(ChatSession).filter(
            ChatSession.id == task.session_id,
            ChatSession.is_deleted == False,  # noqa: E712
        ).first()
        if existing is not None and _session_title_matches(existing.title, task_name):
            return existing.id, False
        if existing is not None:
            old_conv_id = existing.conversation_id  # remember for re-link

    if old_conv_id:
        conv = db.query(AgentConversation).filter(
            AgentConversation.id == old_conv_id,
        ).first()
    else:
        conv = None
    if conv is None:
        conv = AgentConversation(
            agent_name=None,
            title=task_name,
            messages=[],
            status="active",
            created_by_id=getattr(task, "created_by_id", None),
            project_id=task.project_id,
        )
        conv.metadata_ = {
            "source": "backfill_automation_sessions",
            "task_id": task.id,
        }
        db.add(conv)
        db.flush()

    chat = ChatSession(
        title=task_name,
        project_id=task.project_id,
        project=task.project or "global",
        conversation_id=conv.id,
        agent_name=None,
        starred=False,
        last_message_at=_dt.utcnow().isoformat(),
    )
    db.add(chat)
    db.flush()
    task.session_id = chat.id
    db.commit()
    return chat.id, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the changes that would be made without writing to the DB.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="SQLAlchemy URL to connect to (defaults to settings.DATABASE_URL, "
             "i.e. the same DB the backend is using).",
    )
    args = parser.parse_args()

    # ── Settings + DB session ────────────────────────────────────────────
    # Mirror the .env-loading pattern in fix_conversation_titles.py so
    # this script is runnable from any cwd (not just backend/).
    if "DATABASE_URL" not in os.environ:
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".env",
        )
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

    if args.url:
        os.environ["DATABASE_URL"] = args.url

    from app.database import SessionLocal  # noqa: WPS433
    from app.models.automation_task import AutomationTask  # noqa: WPS433
    from app.models.chat_session import ChatSession  # noqa: WPS433

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("backfill_automation_sessions")

    db = SessionLocal()
    try:
        tasks = db.query(AutomationTask).filter(
            AutomationTask.is_deleted == False,  # noqa: E712
        ).order_by(AutomationTask.created_date.asc()).all()

        adopted = 0
        noop = 0
        for task in tasks:
            task_name = (task.name or "Untitled Automation").strip()
            cur_session_title = None
            if task.session_id:
                existing = db.query(ChatSession).filter(
                    ChatSession.id == task.session_id,
                ).first()
                if existing is not None:
                    cur_session_title = existing.title
            matches = _session_title_matches(cur_session_title or "", task_name)
            if matches:
                noop += 1
                log.info(
                    "SKIP task %s (%s) — already on session '%s'",
                    task.id, task_name, cur_session_title,
                )
                continue

            if args.dry_run:
                adopted += 1
                log.info(
                    "DRY-RUN would adopt task %s (%s) — current session='%s'",
                    task.id, task_name, cur_session_title,
                )
                continue

            try:
                new_sid, created = adopt_session_for_task(db, task)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                log.exception(
                    "FAILED task %s (%s): %s", task.id, task_name, exc,
                )
                continue
            if created:
                adopted += 1
                log.info(
                    "ADOPTED task %s (%s) — new session_id=%s (was '%s')",
                    task.id, task_name, new_sid, cur_session_title,
                )
            else:
                noop += 1
        log.info("DONE: %d adopted, %d no-op, %d total", adopted, noop, len(tasks))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
