"""kanban tool — minimal in-process task board.

Persists a kanban board (lists of cards per column) to a JSON file under
``/root/zhanlu/backend/tool_artifacts/kanban.json``. Columns are
configurable; the default is "todo / in_progress / done".
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_KANBAN_FILE = Path(
    os.environ.get("ZHANLU_KANBAN_FILE", "/root/zhanlu/backend/tool_artifacts/kanban.json")
)


def _load() -> dict:
    if not _KANBAN_FILE.exists():
        return {"columns": ["todo", "in_progress", "done"], "cards": {}}
    try:
        return json.loads(_KANBAN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"columns": ["todo", "in_progress", "done"], "cards": {}}


def _save(state: dict) -> None:
    _KANBAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KANBAN_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _kanban(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()
    state = _load()

    if action == "list":
        return {"success": True, "state": state}

    if action == "add_column":
        name = (args.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        if name in state["columns"]:
            return {"success": False, "error": f"Column already exists: {name}"}
        state["columns"].append(name)
        _save(state)
        return {"success": True, "columns": state["columns"]}

    if action == "add_card":
        column = (args.get("column") or "todo").strip()
        title = (args.get("title") or "").strip()
        body = (args.get("body") or "").strip()
        if not title:
            return {"success": False, "error": "title is required"}
        if column not in state["columns"]:
            return {"success": False, "error": f"Unknown column: {column}. Use one of: {state['columns']}"}
        card_id = uuid.uuid4().hex[:8]
        state["cards"][card_id] = {
            "id": card_id,
            "column": column,
            "title": title,
            "body": body,
            "created_at": time.time(),
        }
        _save(state)
        return {"success": True, "card": state["cards"][card_id]}

    if action == "move":
        card_id = (args.get("card_id") or "").strip()
        to_column = (args.get("to_column") or "").strip()
        if card_id not in state["cards"]:
            return {"success": False, "error": f"Unknown card_id: {card_id}"}
        if to_column not in state["columns"]:
            return {"success": False, "error": f"Unknown column: {to_column}"}
        state["cards"][card_id]["column"] = to_column
        _save(state)
        return {"success": True, "card": state["cards"][card_id]}

    if action == "delete":
        card_id = (args.get("card_id") or "").strip()
        if card_id in state["cards"]:
            del state["cards"][card_id]
            _save(state)
        return {"success": True, "message": f"Deleted {card_id}"}

    return {"success": False, "error": f"Unknown action: {action!r}"}


KANBAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kanban",
        "description": (
            "Minimal in-process kanban board for tracking the agent's work. "
            "Actions: list, add_column, add_card, move, delete. State is "
            "persisted to a JSON file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add_column", "add_card", "move", "delete"]},
                "name": {"type": "string", "description": "Column name (for add_column)."},
                "column": {"type": "string", "description": "Column to add the card to (for add_card)."},
                "to_column": {"type": "string", "description": "Target column (for move)."},
                "card_id": {"type": "string", "description": "Card id (for move/delete)."},
                "title": {"type": "string", "description": "Card title (for add_card)."},
                "body": {"type": "string", "description": "Card body / description (for add_card)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="kanban",
    schema=KANBAN_SCHEMA,
    handler=_kanban,
    category="planning",
    toolset="planning",
    description="In-process kanban board for tracking the agent's work.",
    emoji="📋",
    max_result_size_chars=20_000,
)
