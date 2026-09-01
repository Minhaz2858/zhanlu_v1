"""skill_usage tool — record and report skill invocation metrics.

Backed by JSON file at ``/root/zhanlu/backend/tool_artifacts/skill_usage.json``.
Each record: {skill_name, agent_name, timestamp, success, duration_ms, args_hash}.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_FILE = Path(
    os.environ.get("ZHANLU_SKILL_USAGE_FILE", "/root/zhanlu/backend/tool_artifacts/skill_usage.json")
)
_MAX_RECORDS = 5000


def _load() -> list:
    if not _FILE.exists():
        return []
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(records: list) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(records[-_MAX_RECORDS:], ensure_ascii=False, indent=2), encoding="utf-8")


def _hash_args(args: dict) -> str:
    try:
        return hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:12]
    except Exception:
        return "?"


async def _skill_usage(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "report").lower()
    records = _load()

    if action == "record":
        record = {
            "skill_name": args.get("skill_name", "unknown"),
            "agent_name": (context or {}).get("agent_name") if context else None,
            "timestamp": time.time(),
            "success": bool(args.get("success", True)),
            "duration_ms": int(args.get("duration_ms", 0)),
            "args_hash": _hash_args(args.get("arguments", {})),
        }
        records.append(record)
        _save(records)
        return {"success": True, "recorded": True}

    if action == "report":
        # Aggregate by skill_name
        agg: dict = defaultdict(lambda: {"count": 0, "success": 0, "total_ms": 0})
        for r in records:
            s = agg[r["skill_name"]]
            s["count"] += 1
            if r.get("success"):
                s["success"] += 1
            s["total_ms"] += r.get("duration_ms", 0)
        out = []
        for name, s in agg.items():
            out.append({
                "skill_name": name,
                "invocations": s["count"],
                "success_rate": round(s["success"] / s["count"], 3) if s["count"] else 0,
                "avg_duration_ms": int(s["total_ms"] / s["count"]) if s["count"] else 0,
            })
        out.sort(key=lambda x: x["invocations"], reverse=True)
        return {"success": True, "report": out, "total_records": len(records)}

    return {"success": False, "error": f"Unknown action: {action!r}"}


SKILL_USAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_usage",
        "description": (
            "Record a skill invocation or report usage statistics. "
            "Records are capped at 5000 entries (LRU)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["record", "report"]},
                "skill_name": {"type": "string", "description": "Skill name (for record)."},
                "success": {"type": "boolean", "description": "Whether the invocation succeeded (for record)."},
                "duration_ms": {"type": "integer", "description": "Duration in ms (for record)."},
                "arguments": {"type": "object", "description": "Call args (for record, hashed before storage)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="skill_usage",
    schema=SKILL_USAGE_SCHEMA,
    handler=_skill_usage,
    category="skills",
    toolset="skills",
    description="Record/report skill invocation metrics.",
    emoji="📊",
    max_result_size_chars=20_000,
)
