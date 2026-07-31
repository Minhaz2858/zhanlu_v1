"""project_memory tool — shared project-scoped memory across all agents.

DB-backed via ProjectMemory + ProjectMemoryService. All agents within the
same project contribute to one shared memory store.

Uses the frozen snapshot pattern: project memory is loaded at conversation
start (if the agent belongs to a project) and injected into the system
prompt alongside personal AgentMemory. Mid-session writes update the DB
but not the active prompt (preserves prefix cache). The snapshot refreshes
on the next conversation.

Actions: add, read, search, summarize.
Entry types: fact, decision, artifact_ref, conversation_summary, data_insight.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.project_memory import ProjectMemory
from app.services.tool_registry import registry
from app.services.tool_security import scan_memory_content
from app.services.memory.project_memory_service import ProjectMemoryService

logger = logging.getLogger(__name__)

# Char limit for a single entry's content (not the whole store)
ENTRY_CHAR_LIMIT = 1500


def _truncate_for_display(content: str, max_len: int = 200) -> str:
    if len(content) <= max_len:
        return content
    return content[: max_len - 3] + "..."


async def _project_memory_tool(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    action = args.get("action", "")
    entry_type = args.get("entry_type", "fact")
    content = args.get("content", "")
    query = args.get("query", "")

    # Resolve project_id from context
    project_id = (context or {}).get("project_id", "")
    agent_app_id = (context or {}).get("agent_app_id", "")

    if not project_id:
        return {
            "success": False,
            "error": "No project context available. This agent is not assigned to a project.",
        }

    svc = ProjectMemoryService(db)

    # ── read ──────────────────────────────────────────────────────
    if action == "read":
        entries = svc.read_project_context(project_id, limit=30)
        items = [
            {
                "id": e.id,
                "entry_type": e.entry_type,
                "content": _truncate_for_display(e.content),
                "importance": e.importance,
                "usage_count": e.usage_count,
                "agent_app_id": e.agent_app_id,
            }
            for e in entries
        ]
        return {
            "success": True,
            "entries": items,
            "entry_count": len(items),
        }

    # ── add ────────────────────────────────────────────────────────
    elif action == "add":
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}
        if len(content) > ENTRY_CHAR_LIMIT:
            return {
                "success": False,
                "error": f"Content exceeds {ENTRY_CHAR_LIMIT:,} char limit ({len(content):,} chars). "
                         f"Split into multiple shorter entries.",
            }

        # Validate entry_type
        valid_types = {"fact", "decision", "artifact_ref", "conversation_summary", "data_insight"}
        if entry_type not in valid_types:
            return {
                "success": False,
                "error": f"Invalid entry_type '{entry_type}'. Choose from: {', '.join(sorted(valid_types))}.",
            }

        # Injection scan
        is_safe, detected = scan_memory_content(content)
        if not is_safe:
            return {"success": False, "error": f"Content blocked: contains injection patterns: {detected}"}

        entry = svc.write_entry(
            project_id=project_id,
            content=content,
            entry_type=entry_type,
            agent_app_id=agent_app_id,
            user_id=user_id,
            importance=args.get("importance", 0),
        )

        is_duplicate = entry.content != content  # dedup hit
        return {
            "success": True,
            "entry_id": entry.id,
            "entry_type": entry.entry_type,
            "message": "Duplicate skipped (already exists)." if is_duplicate else "Entry added to project memory.",
            "importance": entry.importance,
        }

    # ── search ────────────────────────────────────────────────────
    elif action == "search":
        if not query:
            return {"success": False, "error": "query is required for 'search' action."}
        entries = svc.search_entries(project_id, query, limit=10)
        items = [
            {
                "id": e.id,
                "entry_type": e.entry_type,
                "content": _truncate_for_display(e.content),
                "importance": e.importance,
            }
            for e in entries
        ]
        return {
            "success": True,
            "entries": items,
            "entry_count": len(items),
        }

    # ── summarize ─────────────────────────────────────────────────
    elif action == "summarize":
        entries = svc.read_project_context(project_id, limit=50)
        if not entries:
            return {
                "success": True,
                "summary": "No project memory entries exist yet.",
                "entry_count": 0,
            }

        # Group by type for a structured summary
        by_type: dict[str, list[str]] = {}
        for e in entries:
            by_type.setdefault(e.entry_type or "fact", []).append(e.content)

        lines = [f"Project memory has {len(entries)} entries:"]
        for etype, items in sorted(by_type.items()):
            lines.append(f"\n  [{etype}] ({len(items)} entries)")
            for item in items[:5]:  # show top 5 per type
                lines.append(f"    - {_truncate_for_display(item, 120)}")
            if len(items) > 5:
                lines.append(f"    ... and {len(items) - 5} more")

        return {
            "success": True,
            "summary": "\n".join(lines),
            "entry_count": len(entries),
            "type_breakdown": {k: len(v) for k, v in by_type.items()},
        }

    # ── unknown action ────────────────────────────────────────────
    else:
        return {
            "success": False,
            "error": f"Unknown action '{action}'. Use: add, read, search, summarize",
        }


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

PROJECT_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "project_memory",
        "description": (
            "Access and modify shared project memory — a persistent store visible to ALL agents "
            "working in the same project. Use this to share discoveries, conventions, decisions, "
            "and data insights across agent boundaries.\n\n"
            "ACTIONS:\n"
            "- 'add': save a new entry (provide 'entry_type' and 'content'). "
            "  Duplicates by exact content are automatically skipped.\n"
            "- 'read': view current entries (ranked by importance + recency).\n"
            "- 'search': find entries matching a keyword.\n"
            "- 'summarize': get a structured summary grouped by entry type.\n\n"
            "ENTRY TYPES:\n"
            "- 'fact': something the team should know (environment, constraint)\n"
            "- 'decision': a design decision the team agreed on\n"
            "- 'artifact_ref': pointer to a generated artifact\n"
            "- 'conversation_summary': condensed insight from a chat\n"
            "- 'data_insight': something learned from data\n\n"
            "IMPORTANCE: 0-10 where 10 is critical."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "read", "search", "summarize"],
                    "description": "The action to perform.",
                },
                "entry_type": {
                    "type": "string",
                    "enum": ["fact", "decision", "artifact_ref", "conversation_summary", "data_insight"],
                    "description": "Type of entry (default: fact). Used for 'add' action.",
                },
                "content": {
                    "type": "string",
                    "description": "Entry content. Required for 'add' action. Max 1500 chars.",
                },
                "query": {
                    "type": "string",
                    "description": "Keyword search query. Required for 'search' action.",
                },
                "importance": {
                    "type": "integer",
                    "description": "Importance 0-10 (default 0). Used for 'add' action.",
                    "minimum": 0,
                    "maximum": 10,
                },
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="project_memory",
    schema=PROJECT_MEMORY_SCHEMA,
    handler=_project_memory_tool,
    category="memory",
    enabled_by_default=True,
    description="Shared project-scoped memory across all agents.",
)
