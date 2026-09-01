"""checkpoint_manager tool — save/restore session state for long-running tasks.

Stores snapshots of (todos, agent_state, recent_messages) so a
conversation can be resumed after a crash or manual save. Backed by
JSON files under ``/root/zhanlu/backend/tool_artifacts/checkpoints/``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_DIR = Path(
    os.environ.get("ZHANLU_CHECKPOINT_DIR", "/root/zhanlu/backend/tool_artifacts/checkpoints")
)


async def _checkpoint_manager(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()
    _DIR.mkdir(parents=True, exist_ok=True)

    if action == "list":
        items = []
        for p in sorted(_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                items.append({
                    "checkpoint_id": p.stem,
                    "created_at": meta.get("created_at"),
                    "label": meta.get("label"),
                    "size_bytes": p.stat().st_size,
                })
            except Exception:
                continue
        return {"success": True, "checkpoints": items}

    if action == "save":
        label = (args.get("label") or "").strip() or f"checkpoint-{int(time.time())}"
        cp_id = uuid.uuid4().hex[:12]
        payload = {
            "checkpoint_id": cp_id,
            "label": label,
            "created_at": time.time(),
            "agent_name": (context or {}).get("agent_name") if context else None,
            "conversation_id": (context or {}).get("conversation_id") if context else None,
            "state": args.get("state", {}),
        }
        path = _DIR / f"{cp_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "checkpoint_id": cp_id, "label": label, "path": str(path)}

    if action == "load":
        cp_id = (args.get("checkpoint_id") or "").strip()
        path = _DIR / f"{cp_id}.json"
        if not path.exists():
            return {"success": False, "error": f"Unknown checkpoint_id: {cp_id}"}
        try:
            return {"success": True, "checkpoint": json.loads(path.read_text(encoding="utf-8"))}
        except Exception as exc:
            return {"success": False, "error": f"Failed to load: {exc}"}

    if action == "delete":
        cp_id = (args.get("checkpoint_id") or "").strip()
        path = _DIR / f"{cp_id}.json"
        if path.exists():
            path.unlink()
        return {"success": True, "deleted": cp_id}

    return {"success": False, "error": f"Unknown action: {action!r}"}


CHECKPOINT_MANAGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "checkpoint_manager",
        "description": (
            "Save and restore session state snapshots for long-running "
            "tasks. Useful for resumable workflows and crash recovery."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "save", "load", "delete"]},
                "checkpoint_id": {"type": "string", "description": "Checkpoint id (for load/delete)."},
                "label": {"type": "string", "description": "Human label (for save)."},
                "state": {"type": "object", "description": "Arbitrary JSON state to persist (for save)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="checkpoint_manager",
    schema=CHECKPOINT_MANAGER_SCHEMA,
    handler=_checkpoint_manager,
    category="ux",
    toolset="ux",
    description="Save/restore session state snapshots.",
    emoji="💾",
    max_result_size_chars=50_000,
)
