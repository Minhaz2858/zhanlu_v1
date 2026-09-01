"""todo tool — session-scoped task planning and progress tracking.

DB-backed via AgentTodo model. Each conversation has one AgentTodo row
containing a JSON array of items. Items: {id, content, status}.

Actions: write (with merge=false to replace, merge=true to update by id), read.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.agent_todo import AgentTodo
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


def _get_or_create_todo(db: Session, conversation_id: str, agent_app_id: str | None) -> AgentTodo:
    """Get existing todo record or create a new one."""
    todo = db.query(AgentTodo).filter(
        AgentTodo.conversation_id == conversation_id,
        AgentTodo.is_deleted == False,
    ).first()

    if not todo:
        todo = AgentTodo(
            conversation_id=conversation_id,
            agent_app_id=agent_app_id,
            items=[],
        )
        db.add(todo)
        db.commit()
        db.refresh(todo)
    return todo


def _validate_item(item: dict) -> dict[str, str]:
    """Validate and normalize a todo item."""
    item_id = str(item.get("id", "")).strip() or "?"
    content = str(item.get("content", "")).strip() or "(no description)"
    status = str(item.get("status", "pending")).strip().lower()
    if status not in VALID_STATUSES:
        status = "pending"
    return {"id": item_id, "content": content, "status": status}


def _dedupe_by_id(todos: list[dict]) -> list[dict]:
    """Collapse duplicate ids, keeping the last occurrence."""
    last_index: dict[str, int] = {}
    for i, item in enumerate(todos):
        item_id = str(item.get("id", "")).strip() or "?"
        last_index[item_id] = i
    return [todos[i] for i in sorted(last_index.values())]


def _build_summary(items: list[dict]) -> dict:
    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")
    return {
        "total": len(items),
        "pending": pending,
        "in_progress": in_progress,
        "completed": completed,
        "cancelled": cancelled,
    }


async def _todo_tool(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    ctx = context or {}
    conversation_id = ctx.get("conversation_id", "")
    agent_app_id = ctx.get("agent_app_id")

    if not conversation_id:
        return {"success": False, "error": "No conversation context for todo."}

    todo = _get_or_create_todo(db, conversation_id, agent_app_id)
    items = todo.items or []
    todos_arg = args.get("todos")
    merge = args.get("merge", False)

    if todos_arg is not None:
        # Write mode
        if not merge:
            # Replace entire list
            items = [_validate_item(t) for t in _dedupe_by_id(todos_arg)]
        else:
            # Merge: update existing by id, append new
            existing = {item["id"]: item for item in items}
            for t in _dedupe_by_id(todos_arg):
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue
                if item_id in existing:
                    # Update only provided fields
                    if t.get("content"):
                        existing[item_id]["content"] = str(t["content"]).strip()
                    if t.get("status"):
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    validated = _validate_item(t)
                    existing[validated["id"]] = validated
                    items.append(validated)

        todo.items = items
        db.commit()

    # Always return the full list
    return {
        "success": True,
        "todos": items,
        "summary": _build_summary(items),
    }


# ---------------------------------------------------------------------------
# Snapshot for system prompt injection
# ---------------------------------------------------------------------------

def load_todo_snapshot(db: Session, conversation_id: str) -> str | None:
    """Load active todo items for system prompt injection.

    Returns a formatted string of pending/in_progress items, or None if empty.
    """
    todo = db.query(AgentTodo).filter(
        AgentTodo.conversation_id == conversation_id,
        AgentTodo.is_deleted == False,
    ).first()

    if not todo or not todo.items:
        return None

    markers = {
        "completed": "[x]",
        "in_progress": "[>]",
        "pending": "[ ]",
        "cancelled": "[~]",
    }

    active = [item for item in todo.items if item.get("status") in ("pending", "in_progress")]
    if not active:
        return None

    lines = ["[Your active task list]"]
    for item in active:
        marker = markers.get(item.get("status", "pending"), "[?]")
        lines.append(f"- {marker} {item.get('id', '?')}. {item.get('content', '')} ({item.get('status', 'pending')})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

TODO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "Manage your task list for the current session. Use for complex tasks "
            "with 3+ steps or when the user provides multiple tasks. "
            "Call with no 'todos' parameter to read the current list.\n\n"
            "Writing:\n"
            "- Provide 'todos' array to create/update items\n"
            "- merge=false (default): replace the entire list with a fresh plan\n"
            "- merge=true: update existing items by id, add any new ones\n\n"
            "Each item: {id: string, content: string, "
            "status: pending|in_progress|completed|cancelled}\n"
            "List order is priority. Only ONE item in_progress at a time.\n"
            "Mark items completed immediately when done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Task items to write. Omit to read current list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Unique item identifier"},
                            "content": {"type": "string", "description": "Task description"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "Current status",
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                },
                "merge": {
                    "type": "boolean",
                    "description": "true: update existing items by id, add new ones. false (default): replace the entire list.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}

registry.register(
    name="todo",
    schema=TODO_SCHEMA,
    handler=_todo_tool,
    category="planning",
    enabled_by_default=True,
    description="Task planning and progress tracking.",
)
