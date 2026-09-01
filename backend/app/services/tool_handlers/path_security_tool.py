"""path_security tool — validate that a path stays within an allowed root.

The existing ``app.services.tool_security.validate_path`` covers the
common case (workspace-relative). This tool is a generalized variant that
accepts an explicit allowed root, useful when the agent is operating on
files outside the default workspace (e.g. inside a user-supplied
directory or a project mount).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


def _validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """Return None if path is inside root, or an error string if not."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


async def _path_security(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    path = (args.get("path") or "").strip()
    root = (args.get("root") or "").strip()
    if not path:
        return {"success": False, "error": "path is required"}
    if not root:
        return {"success": False, "error": "root is required (the directory the path must stay within)"}
    p = Path(path)
    r = Path(root)
    if not r.exists():
        return {"success": False, "error": f"Root does not exist: {root}"}
    error = _validate_within_dir(p, r)
    if error:
        return {
            "success": True,        # the check itself succeeded
            "safe": False,
            "error": error,
            "path": str(p),
            "root": str(r),
        }
    return {
        "success": True,
        "safe": True,
        "resolved_path": str(p.resolve()),
        "root": str(r.resolve()),
    }


PATH_SECURITY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "path_security",
        "description": (
            "Check whether a path stays within a specified allowed root "
            "directory (no path traversal). Returns {safe: bool, "
            "resolved_path: str}. Fails closed — symlinks and '..' are "
            "resolved before the check."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to validate."},
                "root": {"type": "string", "description": "The allowed root directory."},
            },
            "required": ["path", "root"],
        },
    },
}


registry.register(
    name="path_security",
    schema=PATH_SECURITY_SCHEMA,
    handler=_path_security,
    category="files",
    toolset="files",
    description="Path-traversal protection: check that a path is within an allowed root.",
    emoji="🔒",
)
