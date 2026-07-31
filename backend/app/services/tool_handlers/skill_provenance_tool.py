"""skill_provenance tool — track the origin of a skill (which tool, who created, when).

Backed by a small JSON file at ``/root/zhanlu/backend/tool_artifacts/skill_provenance.json``.
Each record: {skill_name, source, created_by, created_at, last_used_at, use_count, version}.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_FILE = Path(
    os.environ.get("ZHANLU_PROVENANCE_FILE", "/root/zhanlu/backend/tool_artifacts/skill_provenance.json")
)
_lock = threading.Lock()


def _load() -> dict:
    if not _FILE.exists():
        return {"skills": {}}
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"skills": {}}


def _save(state: dict) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def _skill_provenance(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()
    state = _load()

    if action == "list":
        return {"success": True, "skills": list(state["skills"].values())}

    skill_name = (args.get("skill_name") or "").strip()
    if not skill_name:
        return {"success": False, "error": "skill_name is required"}

    with _lock:
        record = state["skills"].get(skill_name, {
            "skill_name": skill_name,
            "source": "unknown",
            "created_by": "unknown",
            "created_at": None,
            "last_used_at": None,
            "use_count": 0,
            "version": 1,
        })

        if action == "record":
            record["source"] = args.get("source", record["source"])
            record["created_by"] = args.get("created_by", user_id or record["created_by"])
            record["created_at"] = record.get("created_at") or time.time()
            state["skills"][skill_name] = record
            _save(state)
            return {"success": True, "record": record}

        if action == "touch":
            record["last_used_at"] = time.time()
            record["use_count"] = record.get("use_count", 0) + 1
            state["skills"][skill_name] = record
            _save(state)
            return {"success": True, "record": record}

    return {"success": False, "error": f"Unknown action: {action!r}"}


SKILL_PROVENANCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_provenance",
        "description": (
            "Track the origin and usage of a skill. Actions: list, "
            "record (set source/creator), touch (bump use_count and "
            "last_used_at)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "record", "touch"]},
                "skill_name": {"type": "string", "description": "Skill name (required for record/touch)."},
                "source": {"type": "string", "description": "Origin of the skill (record)."},
                "created_by": {"type": "string", "description": "Who created the skill (record)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="skill_provenance",
    schema=SKILL_PROVENANCE_SCHEMA,
    handler=_skill_provenance,
    category="skills",
    toolset="skills",
    description="Track skill origin and usage.",
    emoji="🏷️",
    max_result_size_chars=20_000,
)
