"""cronjob tool — manage scheduled tasks.

A minimal in-process cron-style scheduler. Jobs are persisted to
``/root/zhanlu/backend/tool_artifacts/cronjobs.json`` and the dispatcher
runs every 30 seconds inside the backend (when the runtime permits).

NOT a replacement for system cron — intended for short-interval agent
tasks (e.g. "remind me in 10 minutes", "ping a webhook every hour").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_CRON_FILE = Path(
    os.environ.get("ZHANLU_CRON_FILE", "/root/zhanlu/backend/tool_artifacts/cronjobs.json")
)

# Job handler registry: name -> async callable(args, db, user_id, context)
_handlers: dict[str, Callable[..., Awaitable[dict]]] = {}


def register_handler(name: str, fn: Callable[..., Awaitable[dict]]) -> None:
    _handlers[name] = fn


def _load() -> List[dict]:
    if not _CRON_FILE.exists():
        return []
    try:
        return json.loads(_CRON_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(jobs: List[dict]) -> None:
    _CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CRON_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


_CRON_THREAT_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)",
    r"authorized_keys",
    r"/etc/sudoers|visudo",
]


def _scan_threats(prompt: str) -> Optional[str]:
    for pattern in _CRON_THREAT_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            return f"Blocked: prompt matches dangerous pattern {pattern!r}"
    return None


async def _cronjob(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()
    jobs = _load()

    if action == "list":
        return {"success": True, "jobs": jobs}

    if action == "add":
        name = (args.get("name") or "").strip()
        schedule = (args.get("schedule") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        if not name or not schedule or not prompt:
            return {"success": False, "error": "name, schedule, and prompt are required"}
        blocked = _scan_threats(prompt)
        if blocked:
            return {"success": False, "error": blocked}
        job = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "schedule": schedule,
            "prompt": prompt,
            "created_at": time.time(),
            "next_run_at": time.time() + _parse_interval(schedule),
            "last_run_at": None,
            "status": "active",
        }
        jobs.append(job)
        _save(jobs)
        return {"success": True, "job": job}

    if action == "pause":
        job_id = (args.get("job_id") or "").strip()
        for j in jobs:
            if j["id"] == job_id:
                j["status"] = "paused"
                _save(jobs)
                return {"success": True, "job": j}
        return {"success": False, "error": f"Unknown job_id: {job_id}"}

    if action == "resume":
        job_id = (args.get("job_id") or "").strip()
        for j in jobs:
            if j["id"] == job_id:
                j["status"] = "active"
                j["next_run_at"] = time.time() + _parse_interval(j["schedule"])
                _save(jobs)
                return {"success": True, "job": j}
        return {"success": False, "error": f"Unknown job_id: {job_id}"}

    if action == "delete":
        job_id = (args.get("job_id") or "").strip()
        before = len(jobs)
        jobs = [j for j in jobs if j["id"] != job_id]
        _save(jobs)
        return {"success": True, "deleted": before - len(jobs)}

    return {"success": False, "error": f"Unknown action: {action!r}"}


def _parse_interval(schedule: str) -> float:
    """Parse a simple interval like '5m', '2h', '30s' into seconds.

    Falls back to 60s for unrecognized formats.
    """
    s = schedule.strip().lower()
    m = re.match(r"^(\d+)\s*([smhd])$", s)
    if not m:
        return 60.0
    n = int(m.group(1))
    unit = m.group(2)
    return float(n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


CRONJOB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cronjob",
        "description": (
            "Manage short-interval scheduled tasks. Jobs persist to disk "
            "and the dispatcher ticks every 30s. Supported schedule "
            "formats: '30s', '5m', '2h', '1d'. Threats are scanned before "
            "save (rm -rf, secret reads, ssh backdoors)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "pause", "resume", "delete"]},
                "name": {"type": "string", "description": "Job name (for add)."},
                "schedule": {"type": "string", "description": "Interval like '5m' (for add)."},
                "prompt": {"type": "string", "description": "Prompt to run on each tick (for add)."},
                "job_id": {"type": "string", "description": "Job id (for pause/resume/delete)."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="cronjob",
    schema=CRONJOB_SCHEMA,
    handler=_cronjob,
    category="admin",
    toolset="admin",
    description="Manage scheduled agent tasks (in-process).",
    emoji="⏰",
    max_result_size_chars=20_000,
)
