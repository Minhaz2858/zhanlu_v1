"""
One-shot script to re-derive AgentConversation titles from the first user
message.

Background
----------
The chat page used to send ``metadata: { name: activeAgent.name, ... }``
which the backend (``agents.py`` line 1854-1855) silently treated as the
conversation title. So every conversation created via the chat page
inherited the agent's name (e.g. "general_assistant") as its title,
making the Project Detail "Recent Chats" list unreadable.

The fix going forward is in Chat.jsx: the chat page now sends a
top-level ``title`` field derived from the first user message
(trimmed, whitespace-normalized, truncated to 60 chars). New
conversations get meaningful titles automatically.

This script backfills the same logic onto EXISTING conversations that
were created before the fix. It does NOT delete or merge conversations
— that's a separate concern (``cleanup_duplicate_conversations.py``).

What it does
------------
For every active ``AgentConversation`` row whose title is:
  - null / empty
  - the default ``"New Conversation"``
  - equal to the agent's name (the bug case — title == agent_name)
… this script extracts the first user message from the ``messages`` JSON,
normalizes it the same way the frontend does (trim, collapse whitespace,
slice to 60 chars), and writes it back as the title.

Rows with already-meaningful titles are left untouched.

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
    PYTHONPATH=. python scripts/fix_conversation_titles.py --dry-run
    PYTHONPATH=. python scripts/fix_conversation_titles.py

    # Override the DB URL for a one-off run:
    PYTHONPATH=. python scripts/fix_conversation_titles.py \\
        --url postgresql+psycopg2://user:pass@host:5432/dbname

The script is idempotent — running it again after a successful apply
will report "0 rows to update" because the broken titles are gone.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Optional

# Mirror the frontend's title-derivation logic exactly so the new
# titles match what new conversations will look like.
MAX_TITLE_LEN = 60
WHITESPACE_RE = re.compile(r"\s+")

# Titles we consider "broken" and worth re-deriving.
DEFAULT_TITLES = {"", "New Conversation", "Untitled", "未命名对话"}


def derive_title(messages_data: Optional[Any]) -> Optional[str]:
    """Extract the first user message and normalize it as a title.

    Returns ``None`` if no usable user message is found (caller should
    skip the row rather than overwriting the title with an empty
    string).

    Accepts both raw JSON strings and already-parsed lists — the
    SQLAlchemy ``JSON`` type auto-parses the column on Postgres
    (returns a list) but returns the raw string on SQLite. The
    earlier version only handled strings and silently returned
    ``None`` for every Postgres row, which is why the original
    dry-run reported "0 rows to update" against the real DB.
    """
    if not messages_data:
        return None
    if isinstance(messages_data, str):
        try:
            messages = json.loads(messages_data)
        except (json.JSONDecodeError, TypeError):
            return None
    elif isinstance(messages_data, list):
        messages = messages_data
    else:
        return None
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "human"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            # Some clients store content as a list of parts
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if not isinstance(content, str):
            continue
        # Mirror the frontend: trim, collapse whitespace, slice.
        normalized = WHITESPACE_RE.sub(" ", content.strip())
        if not normalized:
            continue
        if len(normalized) > MAX_TITLE_LEN:
            normalized = normalized[:MAX_TITLE_LEN]
        return normalized
    return None


def is_title_broken(title: Optional[str], agent_name: Optional[str]) -> bool:
    """True if the row's title should be re-derived from its first message."""
    if title is None or not title.strip():
        return True
    if title in DEFAULT_TITLES:
        return True
    if agent_name and title == agent_name:
        # The bug case: title was set to the agent name because
        # metadata.name was misused as the title.
        return True
    return False


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
        # ── 2. Find rows that need a title re-derivation ────────────────
        rows = (
            db.query(AgentConversation)
            .filter(AgentConversation.is_deleted == False)  # noqa: E712
            .order_by(AgentConversation.updated_date.desc())
            .all()
        )
        total = len(rows)
        changes: list[tuple[str, Optional[str], str]] = []
        skipped_meaningful = 0
        no_user_message = 0

        for r in rows:
            if not is_title_broken(r.title, r.agent_name):
                skipped_meaningful += 1
                continue
            new_title = derive_title(r.messages)
            if not new_title:
                no_user_message += 1
                continue
            changes.append((r.id, r.title, new_title))

        # ── 3. Apply or preview ──────────────────────────────────────────
        if args.dry_run:
            print(f"[DRY-RUN] Would update {len(changes)} rows "
                  f"(out of {total} active)")
        else:
            for conv_id, _old, new_title in changes:
                conv = db.query(AgentConversation).filter(
                    AgentConversation.id == conv_id
                ).one()
                conv.title = new_title
            db.commit()
            print(f"Updated {len(changes)} rows (out of {total} active)")

        # ── 4. Audit log ─────────────────────────────────────────────────
        if changes:
            print()
            print(f"{'id':<38} {'old title':<28} -> new title")
            print("-" * 100)
            for conv_id, old, new in changes:
                old_display = (old if old is not None else "<NULL>")[:27]
                new_display = new[:60]
                print(f"{conv_id:<38} {old_display:<28} -> {new_display}")
        print()
        print("Summary:")
        print(f"  total active rows:              {total}")
        if args.dry_run:
            print(f"  would update:                   {len(changes)}")
        else:
            print(f"  updated:                        {len(changes)}")
        print(f"  already had meaningful title:   {skipped_meaningful}")
        print(f"  no user message to derive from: {no_user_message}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
