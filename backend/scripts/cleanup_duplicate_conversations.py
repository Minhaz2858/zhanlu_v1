"""
One-shot script to clean up AgentConversation duplicates left over from
the session-reuse bug.

Background
----------
Before the fix in Chat.jsx, ``handleSend`` hardcoded
``sessionId: null`` in ``handleAgentSend``, so every user message
created a brand-new AgentConversation. The Project Detail "Recent
Chats" list therefore showed many duplicate rows per session — one
per message — and the user explicitly approved removing the old
residue ("we can also remove the all old chat sessions it's not
important").

The fix going forward is in Chat.jsx: ``handleSend`` now passes the
real ``sessionId``, so new conversations don't have this problem.

What this script does
---------------------
For each ``(project_id, agent_name)`` group that has more than one
ACTIVE conversation (``is_deleted = 0``), it keeps the most recently
updated row and soft-deletes the rest (``is_deleted = 1``,
``updated_date`` bumped so the audit trail is visible).

Soft-delete (not hard-delete) is intentional: the rows can still be
recovered with a manual UPDATE if needed. The cleanup is also
idempotent — re-running the script after a successful apply will
report "0 rows to soft-delete".

Decisions made (and why)
------------------------
* Keep the MOST RECENTLY UPDATED row per group. The most recent
  conversation is the one the user is most likely to want to keep
  (they were just working in it). Older rows in the same group are
  the session-reuse residue.

* Group key is ``(project_id, agent_name)``. The session-reuse bug
  produced duplicates that share both fields, so this is the right
  granularity. Standalone conversations (groups of 1) are left
  untouched — they're not duplicates of anything.

* Conversations where ``agent_name`` is NULL or empty are NOT
  grouped (each is its own "group of 1"). The session-reuse bug
  always wrote an agent_name, so NULL/empty means it came from
  somewhere else (SDK agent_builder, manual insert, etc.) and we
  shouldn't risk deleting it.

* Conversations with no ``project_id`` (NULL) are grouped together
  with other NULL-project conversations of the same agent_name. If
  the user had 5 ungrouped "Data Analyst" sessions, the script keeps
  the most recent and soft-deletes the other 4. This is aggressive
  but matches the user's "it's not important" instruction.

Database
--------
Uses SQLAlchemy + the backend's existing ``app.models.AgentConversation``
so the same script works on both SQLite (local dev) and Postgres
(production). Set ``DATABASE_URL`` to point at the target DB, or use
``--url`` to override for a single run.

Usage
-----
::

    cd /root/zhanlu/backend
    source venv/bin/activate
    PYTHONPATH=. python scripts/cleanup_duplicate_conversations.py --dry-run
    PYTHONPATH=. python scripts/cleanup_duplicate_conversations.py

    # Override the DB URL for a one-off run:
    PYTHONPATH=. python scripts/cleanup_duplicate_conversations.py \\
        --url postgresql+psycopg2://user:pass@host:5432/dbname
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the soft-deletes without writing to the DB.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="SQLAlchemy URL to connect to (defaults to settings.DATABASE_URL, "
             "i.e. the same DB the backend is using). Useful for one-off runs "
             "against a different DB without editing .env.",
    )
    args = parser.parse_args()

    # ── 1. Settings + DB session ─────────────────────────────────────────
    # ``app.config.Settings`` reads ``.env`` from the *current working
    # directory*, not from the script's directory. If the user runs
    # the script from elsewhere, ``.env`` won't be found and
    # ``Settings()`` will raise "DATABASE_URL required". To make the
    # scripts runnable from anywhere, we pre-load ``.env`` from the
    # backend directory (the one that contains this script's
    # ``../../app/``) if it's not already in the environment.
    import os  # noqa: WPS433 — local import
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

    from app.config import settings  # noqa: WPS433 — script import
    from app.models.agent_conversation import AgentConversation  # noqa: WPS433

    db_url = args.url or settings.DATABASE_URL
    print(f"DB URL: {db_url}")

    from sqlalchemy import create_engine  # noqa: WPS433
    from sqlalchemy.orm import sessionmaker  # noqa: WPS433

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        # ── 2. Pull all active rows and group by (project_id, agent_name) ──
        rows: list[Any] = (
            db.query(AgentConversation)
            .filter(AgentConversation.is_deleted == False)  # noqa: E712
            .all()
        )
        total = len(rows)

        groups: dict[tuple[Optional[str], str], list[Any]] = defaultdict(list)
        ungrouped = 0  # rows with no agent_name — left untouched
        for r in rows:
            agent = r.agent_name
            if not agent:
                # Don't risk deleting rows with no agent_name
                ungrouped += 1
                continue
            groups[(r.project_id, agent)].append(r)

        # ── 3. Pick the survivor per group, mark the rest for delete ─────
        to_delete: list[tuple[str, str, str, Optional[str], str]] = []
        # (id, agent_name, project_id, old_title, updated_date)
        survivors: list[tuple[str, str, str, str]] = []
        # (id, agent_name, project_id, survivor_title)

        for (project_id, agent_name), members in groups.items():
            if len(members) <= 1:
                continue
            # Sort ascending by (updated_date, id) so the LAST
            # element has the latest updated_date (ISO 8601 strings
            # compare lexicographically == chronologically).
            # Tie-break: lowest id wins, so the survivor is
            # deterministic across runs.
            def _sort_key(m: Any) -> tuple[str, str]:
                updated = m.updated_date
                if isinstance(updated, datetime):
                    # Postgres returns datetime objects; normalize to
                    # ISO so the sort is stable across SQLite/Postgres.
                    updated = updated.isoformat()
                return (updated or "", m.id)

            members_sorted = sorted(members, key=_sort_key)
            survivor = members_sorted[-1]
            duplicates = members_sorted[:-1]
            survivors.append(
                (survivor.id, agent_name, project_id or "<ungrouped>",
                 survivor.title or "<NULL>")
            )
            for d in duplicates:
                dup_updated = d.updated_date
                if isinstance(dup_updated, datetime):
                    dup_updated = dup_updated.isoformat()
                to_delete.append(
                    (d.id, agent_name, project_id or "<ungrouped>",
                     d.title, dup_updated or "<no date>")
                )

        # ── 4. Apply or preview ──────────────────────────────────────────
        if args.dry_run:
            print(f"[DRY-RUN] Would soft-delete {len(to_delete)} duplicate rows "
                  f"out of {total} active")
        else:
            now_iso = datetime.utcnow().isoformat() + "Z"
            for conv_id, _agent, _project, _title, _updated in to_delete:
                conv = db.query(AgentConversation).filter(
                    AgentConversation.id == conv_id
                ).one()
                conv.is_deleted = True
                conv.updated_date = now_iso
            db.commit()
            print(f"Soft-deleted {len(to_delete)} duplicate rows "
                  f"(out of {total} active)")

        # ── 5. Per-group audit log ───────────────────────────────────────
        if survivors:
            print()
            print("=== Groups with duplicates (kept survivor) ===")
            print(f"{'project':<14} {'agent':<22} {'survivor_id':<38} survivor_title")
            print("-" * 100)
            for sid, agent, project, title in survivors:
                print(f"{project:<14} {agent[:21]:<22} {sid:<38} {title[:40]}")

        if to_delete:
            print()
            print("=== Rows marked for soft-delete ===")
            print(f"{'id':<38} {'project':<14} {'agent':<22} {'updated_date':<28} title")
            print("-" * 130)
            for did, agent, project, title, updated in to_delete:
                title_disp = (title or "<NULL>")[:30]
                print(f"{did:<38} {project[:13]:<14} {agent[:21]:<22} {updated[:27]:<28} {title_disp}")

        print()
        print("Summary:")
        print(f"  total active rows:       {total}")
        print(f"  groups with duplicates:  {len(survivors)}")
        print(f"  rows to soft-delete:     {len(to_delete)}")
        print(f"  rows not grouped (no agent_name): {ungrouped}")
        survivors_count = total - len(to_delete)
        print(f"  survivors (kept):        {survivors_count}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
