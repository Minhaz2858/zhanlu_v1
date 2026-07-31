"""env_passthrough tool — list/manage the env-var allowlist for sandboxed code.

When execute_code or terminal_run runs a child process, the sandbox
strips sensitive env vars (API keys, tokens). This tool exposes the
allowlist so the user (via the agent) can opt specific vars back in.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional, Set

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

# Session-scoped allowlist (per-process — zhanlu runs single-tenant in dev)
_lock = threading.Lock()
_allowed: Set[str] = set()


def get_allowed() -> Set[str]:
    with _lock:
        return set(_allowed)


def is_passthrough(name: str) -> bool:
    return name in get_allowed()


# Variable name validation: must be uppercase snake_case
import re
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


async def _env_passthrough(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()
    var = (args.get("name") or "").strip()
    if action == "list":
        return {
            "success": True,
            "allowed": sorted(get_allowed()),
            "note": "These env vars pass through to sandboxed execute_code and terminal runs.",
        }
    if not _NAME_RE.match(var):
        return {"success": False, "error": f"Invalid env var name: {var!r}"}
    if action == "add":
        with _lock:
            _allowed.add(var)
        return {"success": True, "message": f"Added {var} to passthrough", "allowed": sorted(get_allowed())}
    if action == "remove":
        with _lock:
            _allowed.discard(var)
        return {"success": True, "message": f"Removed {var} from passthrough", "allowed": sorted(get_allowed())}
    return {"success": False, "error": f"Unknown action: {action!r}. Use 'list', 'add', or 'remove'."}


ENV_PASSTHROUGH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "env_passthrough",
        "description": (
            "Manage the env-var passthrough allowlist for sandboxed code "
            "execution (execute_code, terminal_run). Vars on the list are "
            "passed to child processes; others are stripped for safety."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove"]},
                "name": {"type": "string", "description": "The env var name (uppercase snake_case). Required for 'add'/'remove'."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="env_passthrough",
    schema=ENV_PASSTHROUGH_SCHEMA,
    handler=_env_passthrough,
    category="admin",
    toolset="admin",
    description="Manage env-var passthrough allowlist for sandboxed code.",
    emoji="🔓",
)
