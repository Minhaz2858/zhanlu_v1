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
) -> AgentMemory:
    """Get existing memory record or create a new one."""
    query = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.target == target,
        AgentMemory.is_deleted == False,
    )
    if user_id:
        query = query.filter(AgentMemory.user_id == user_id)
    else:
        query = query.filter(AgentMemory.user_id.is_(None))

    mem = query.first()
    if not mem:
        mem = AgentMemory(
            agent_app_id=agent_app_id,
            user_id=user_id,
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

    if target not in ("memory", "user"):
        return {"success": False, "error": f"Invalid target '{target}'. Use 'memory' or 'user'."}

    mem = _get_or_create_memory(db, agent_app_id, user_id, target)
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

def load_memory_snapshot(db: Session, agent_app_id: str, user_id: str | None) -> dict[str, str]:
    """Load frozen memory snapshot for system prompt injection.

    Returns dict with 'memory' and 'user' keys containing formatted blocks
    (or empty strings if no entries). Called once at conversation start.
    """
    result = {"memory": "", "user": ""}
    for target in ("memory", "user"):
        query = db.query(AgentMemory).filter(
            AgentMemory.agent_app_id == agent_app_id,
            AgentMemory.target == target,
            AgentMemory.is_deleted == False,
        )
        if user_id:
            query = query.filter(AgentMemory.user_id == user_id)
        else:
            query = query.filter(AgentMemory.user_id.is_(None))
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
