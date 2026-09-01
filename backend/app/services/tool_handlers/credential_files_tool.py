"""credential_files tool — list/inspect mounted credential files for sandboxes.

Lets the agent enumerate the credential files that will be mounted into
remote sandboxes. Does not return the contents (which would defeat the
purpose of the sandbox) — only metadata.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_registered: Dict[str, str] = {}  # relative_path -> container_base


def register(relative_path: str, container_base: str = "/root/.zhanlu") -> bool:
    p = Path(relative_path)
    if not p.exists() or not p.is_file():
        return False
    with _lock:
        _registered[relative_path] = container_base
    return True


def get_all() -> List[dict]:
    with _lock:
        out = []
        for rel, base in _registered.items():
            p = Path(rel)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append({
                "host_path": rel,
                "container_path": f"{base}/{p.name}",
                "size_bytes": size,
            })
        return out


async def _credential_files(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "list").lower()
    if action == "list":
        return {"success": True, "credentials": get_all()}
    if action == "register":
        path = (args.get("path") or "").strip()
        if not path:
            return {"success": False, "error": "path is required"}
        ok = register(path)
        if not ok:
            return {"success": False, "error": f"File not found: {path}"}
        return {"success": True, "message": f"Registered {path}"}
    return {"success": False, "error": f"Unknown action {action!r}"}


CREDENTIAL_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "credential_files",
        "description": (
            "List or register credential files that will be mounted into "
            "remote sandboxes. The file CONTENTS are not returned — only "
            "metadata (host path, container path, size)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "register"]},
                "path": {"type": "string", "description": "Host path of a credential file. Required for action='register'."},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="credential_files",
    schema=CREDENTIAL_FILES_SCHEMA,
    handler=_credential_files,
    category="admin",
    toolset="admin",
    description="Manage credential file mounts for sandboxes.",
    emoji="🗝️",
)
