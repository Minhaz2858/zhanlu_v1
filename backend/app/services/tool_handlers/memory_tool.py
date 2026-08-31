"""memory tool — persistent agent memory across conversations.

DB-backed via AgentMemory model. Two targets:
  - "memory": agent's personal notes (environment facts, conventions, lessons)
  - "user": what the agent knows about the user (preferences, style, role)

Uses the frozen snapshot pattern: content is loaded at conversation start
and injected into the system prompt. Mid-session writes update the DB but
not the active prompt (preserves prefix cache). The snapshot refreshes
on the next conversation.

Actions: add, replace, remove, read.
Entry delimiter: § (section sign). Entries can be multiline.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.agent_memory import AgentMemory
from app.services.tool_registry import registry
from app.services.tool_security import scan_memory_content

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n§\n"

# Character limits per target (not tokens — model-independent)
MEMORY_CHAR_LIMIT = 2200
USER_CHAR_LIMIT = 1375


def _get_or_create_memory(
    db: Session,
    agent_app_id: str,
    user_id: str | None,
    target: str,
    project_id: str | None = None,
) -> AgentMemory:
    """Get existing memory record or create a new one.

    Project scoping (2026-08-05): when ``project_id`` is provided, the
    lookup matches rows with that project_id; NULL project_id rows
    are matched only for the ``target='user'`` case (user profile is
    intentionally cross-project) or when the caller explicitly asks
    for the legacy "no project" bucket (``project_id == ""`` is
    treated as NULL). This prevents the cross-project memory leak
    that surfaced when a note taken in one project got injected
    into every other project's system prompt.
    """
    query = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.target == target,
        AgentMemory.is_deleted == False,
    )
    if user_id:
        query = query.filter(AgentMemory.user_id == user_id)
    else:
        query = query.filter(AgentMemory.user_id.is_(None))

    # Project filter: user-profile rows are always cross-project
    # (target='user' is about WHO the user is); for target='memory'
    # we use STRICT match — no NULL fallback when an active project
    # is set, so a write in project-A attaches to (or creates) the
    # project-A row, never the legacy NULL bucket. The NULL bucket
    # is only matched when the caller has no project context (e.g.
    # the "Ungrouped" chat). This prevents two bugs:
    #   1. Reusing a legacy NULL-bucket row when a project-scoped
    #      note is being written (would attach the new note to the
    #      legacy row and silently make it cross-project again).
    #   2. Cross-project leakage via the NULL fallback at read time
    #      (the Q2 2026 sales report that surfaced in every
    #      project).
    if target == "user":
        pass  # user profile is always cross-project
    elif project_id is None:
        # Active project is None — only match the legacy NULL bucket.
        query = query.filter(AgentMemory.project_id.is_(None))
    else:
        # Active project is set — strict match. If no row exists
        # for this project yet, a new one is created below.
        query = query.filter(AgentMemory.project_id == project_id)

    mem = query.first()
    if not mem:
        mem = AgentMemory(
            agent_app_id=agent_app_id,
            user_id=user_id,
            project_id=project_id if target != "user" else None,
            target=target,
            content="",
            char_count=0,
        )
        db.add(mem)
        db.commit()
        db.refresh(mem)
    return mem


def _parse_entries(content: str) -> list[str]:
    """Split stored content into entries."""
    if not content or not content.strip():
        return []
    entries = [e.strip() for e in content.split(ENTRY_DELIMITER)]
    return [e for e in entries if e]


def _serialize_entries(entries: list[str]) -> str:
    """Join entries into stored content."""
    return ENTRY_DELIMITER.join(entries) if entries else ""


def _char_count(entries: list[str]) -> int:
    if not entries:
        return 0
    return len(ENTRY_DELIMITER.join(entries))


def _char_limit(target: str) -> int:
    return USER_CHAR_LIMIT if target == "user" else MEMORY_CHAR_LIMIT


def _success_response(target: str, entries: list[str], message: str = None) -> dict:
    current = _char_count(entries)
    limit = _char_limit(target)
    pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
    resp = {
        "success": True,
        "target": target,
        "entries": entries,
        "usage": f"{pct}% — {current:,}/{limit:,} chars",
        "entry_count": len(entries),
    }
    if message:
        resp["message"] = message
    return resp


async def _memory_tool(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    action = args.get("action", "")
    target = args.get("target", "memory")
    content = args.get("content", "")
    old_text = args.get("old_text", "")

    # Resolve agent_app_id from context
    agent_app_id = (context or {}).get("agent_app_id", "default")
    if not agent_app_id:
        agent_app_id = "default"

    # Resolve the active project_id from the tool context (set by
    # agent_tools.py when the LLM invokes the memory tool). This is
    # what gets stamped on the AgentMemory row so a note taken in
    # "Data Analysis" stays inside that project.
    project_id = (context or {}).get("project_id")
    # Treat empty string as None to match how the rest of the
    # codebase handles the "no project" case.
    if project_id == "":
        project_id = None

    if target not in ("memory", "user"):
        return {"success": False, "error": f"Invalid target '{target}'. Use 'memory' or 'user'."}

    mem = _get_or_create_memory(db, agent_app_id, user_id, target, project_id=project_id)
    entries = _parse_entries(mem.content)

    if action == "read":
        return _success_response(target, entries)

    elif action == "add":
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Injection scan
        is_safe, detected = scan_memory_content(content)
        if not is_safe:
            return {"success": False, "error": f"Content blocked: contains injection patterns: {detected}"}

        # Reject exact duplicates
        if content in entries:
            return _success_response(target, entries, "Entry already exists (no duplicate added).")

        # Check char limit
        new_entries = entries + [content]
        new_total = _char_count(new_entries)
        limit = _char_limit(target)
        if new_total > limit:
            current = _char_count(entries)
            return {
                "success": False,
                "error": f"Memory at {current:,}/{limit:,} chars. Adding this entry "
                         f"({len(content)} chars) would exceed the limit. "
                         f"Replace or remove existing entries first.",
                "current_entries": entries,
                "usage": f"{current:,}/{limit:,}",
            }

        entries.append(content)
        mem.content = _serialize_entries(entries)
        mem.char_count = _char_count(entries)
        db.commit()
        return _success_response(target, entries, "Entry added.")

    elif action == "replace":
        old_text = old_text.strip()
        new_content = content.strip()
        if not old_text:
            return {"success": False, "error": "old_text is required for 'replace' action."}
        if not new_content:
            return {"success": False, "error": "content is required for 'replace' action. Use 'remove' to delete."}

        # Injection scan
        is_safe, detected = scan_memory_content(new_content)
        if not is_safe:
            return {"success": False, "error": f"Content blocked: contains injection patterns: {detected}"}

        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text}'."}
        if len(matches) > 1:
            unique_texts = {e for _, e in matches}
            if len(unique_texts) > 1:
                previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                return {"success": False, "error": f"Multiple entries matched '{old_text}'. Be more specific.", "matches": previews}

        idx = matches[0][0]
        limit = _char_limit(target)
        test_entries = entries.copy()
        test_entries[idx] = new_content
        new_total = _char_count(test_entries)
        if new_total > limit:
            return {"success": False, "error": f"Replacement would put memory at {new_total:,}/{limit:,} chars. Shorten or remove other entries first."}

        entries[idx] = new_content
        mem.content = _serialize_entries(entries)
        mem.char_count = _char_count(entries)
        db.commit()
        return _success_response(target, entries, "Entry replaced.")

    elif action == "remove":
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text is required for 'remove' action."}

        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return {"success": False, "error": f"No entry matched '{old_text}'."}
        if len(matches) > 1:
            unique_texts = {e for _, e in matches}
            if len(unique_texts) > 1:
                previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                return {"success": False, "error": f"Multiple entries matched '{old_text}'. Be more specific.", "matches": previews}

        idx = matches[0][0]
        entries.pop(idx)
        mem.content = _serialize_entries(entries)
        mem.char_count = _char_count(entries)
        db.commit()
        return _success_response(target, entries, "Entry removed.")

    else:
        return {"success": False, "error": f"Unknown action '{action}'. Use: add, replace, remove, read"}


# ---------------------------------------------------------------------------
# Frozen snapshot for system prompt injection
# ---------------------------------------------------------------------------

def load_memory_snapshot(
    db: Session,
    agent_app_id: str,
    user_id: str | None,
    project_id: str | None = None,
) -> dict[str, str]:
    """Load frozen memory snapshot for system prompt injection.

    Returns dict with 'memory' and 'user' keys containing formatted blocks
    (or empty strings if no entries). Called once at conversation start.

    Project scoping (2026-08-05): ``target='user'`` rows (the user
    profile) are always cross-project — they describe WHO the user
    is, not what they did in a specific project. ``target='memory'``
    rows are project-scoped: when ``project_id`` is set, the query
    matches ONLY the active project (strict — no NULL fallback) so
    a note taken in one project cannot leak into another.
    When ``project_id`` is None (the "Ungrouped" chat), **no**
    ``target='memory'`` rows are loaded at all. This prevents legacy
    pre-project-scoping memories (which all have project_id=NULL) from
    leaking into every ungrouped chat. Without this guard, a note like
    "Q2 2026 sales report" (written before the schema column existed)
    would appear in every project's system prompt.
    """
    result = {"memory": "", "user": ""}
    for target in ("memory", "user"):
        # Skip agent memory notes entirely in ungrouped chats to prevent
        # cross-project leakage of legacy pre-scoping memories.
        if target == "memory" and project_id is None:
            continue
        query = db.query(AgentMemory).filter(
            AgentMemory.agent_app_id == agent_app_id,
            AgentMemory.target == target,
            AgentMemory.is_deleted == False,
        )
        if user_id:
            query = query.filter(AgentMemory.user_id == user_id)
        else:
            query = query.filter(AgentMemory.user_id.is_(None))
        # Project filter: user profile is always cross-project;
        # agent notes are project-scoped strictly (no NULL fallback
        # when an active project is set — that's what was leaking
        # the Q2 2026 report into every project).
        if target == "user":
            pass
        elif project_id is None:
            # Already handled by the continue above, but kept for clarity.
            query = query.filter(AgentMemory.project_id.is_(None))
        else:
            query = query.filter(AgentMemory.project_id == project_id)
        mem = query.first()
        if mem and mem.content:
            entries = _parse_entries(mem.content)
            result[target] = _render_block(target, entries)
    return result


def _render_block(target: str, entries: list[str]) -> str:
    """Render a system prompt block with header."""
    if not entries:
        return ""
    limit = _char_limit(target)
    content = ENTRY_DELIMITER.join(entries)
    current = len(content)
    pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
    if target == "user":
        header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
    else:
        header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
    sep = "=" * 46
    return f"{sep}\n{header}\n{sep}\n{content}"


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Save durable information to persistent memory that survives across sessions. "
            "Memory is injected into future conversations, so keep it compact and focused on facts "
            "that will still matter later.\n\n"
            "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
            "- User corrects you or says 'remember this' / 'don't do that again'\n"
            "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
            "- You discover something about the environment (OS, installed tools, project structure)\n"
            "- You learn a convention, API quirk, or workflow specific to this user's setup\n\n"
            "TWO TARGETS:\n"
            "- 'user': who the user is -- name, role, preferences, communication style\n"
            "- 'memory': your notes -- environment facts, project conventions, tool quirks\n\n"
            "ACTIONS: add (new entry), replace (update existing -- old_text identifies it), "
            "remove (delete -- old_text identifies it), read (view current entries).\n\n"
            "Do NOT save task progress or temporary state to memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "remove", "read"],
                    "description": "The action to perform.",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
                },
                "content": {
                    "type": "string",
                    "description": "The entry content. Required for 'add' and 'replace'.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Short unique substring identifying the entry to replace or remove.",
                },
            },
            "required": ["action", "target"],
        },
    },
}

registry.register(
    name="memory",
    schema=MEMORY_SCHEMA,
    handler=_memory_tool,
    category="memory",
    enabled_by_default=True,
    description="Persistent agent memory across conversations.",
)
