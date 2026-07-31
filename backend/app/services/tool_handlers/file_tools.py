"""read_file and write_file tools — file operations scoped to workspace.

Files are confined to settings.AGENT_WORKSPACE_DIR to prevent path traversal.
read_file returns file content (truncated for large files).
write_file uses atomic temp-file + os.replace pattern.
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_registry import registry
from app.services.tool_security import redact_secrets, truncate_output, validate_path

logger = logging.getLogger(__name__)

# Max chars per read to protect context window
MAX_READ_CHARS = 50_000


async def _read_file(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    file_path = args.get("file_path", "").strip()
    offset = args.get("offset", 0)
    limit = args.get("limit", 0)  # 0 = read all (up to MAX_READ_CHARS)

    if not file_path:
        return {"success": False, "error": "file_path is required"}

    try:
        resolved = validate_path(file_path, settings.workspace_path)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if not resolved.exists():
        return {"success": False, "error": f"File not found: {file_path}"}
    if not resolved.is_file():
        return {"success": False, "error": f"Not a file: {file_path}"}

    # Check file size
    file_size = resolved.stat().st_size
    if file_size > 1_000_000:  # 1MB
        return {
            "success": False,
            "error": f"File is {file_size:,} bytes — too large to read at once. "
                     f"Use offset and limit parameters to read sections.",
        }

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"success": False, "error": f"Failed to read file: {e}"}

    # Apply offset and limit
    if offset > 0:
        lines = content.split("\n")
        lines = lines[offset:]
        if limit > 0:
            lines = lines[:limit]
        content = "\n".join(lines)
    elif limit > 0:
        lines = content.split("\n")
        content = "\n".join(lines[:limit])

    # Truncate for context window
    content = truncate_output(content, MAX_READ_CHARS)
    content = redact_secrets(content)

    # File hash for dedup (agents can skip re-reading unchanged files)
    file_hash = hashlib.md5(content.encode()).hexdigest()

    return {
        "success": True,
        "file_path": file_path,
        "content": content,
        "size": file_size,
        "hash": file_hash,
        "lines": content.count("\n") + 1,
    }


async def _write_file(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    file_path = args.get("file_path", "").strip()
    content = args.get("content", "")

    if not file_path:
        return {"success": False, "error": "file_path is required"}
    if content is None:
        return {"success": False, "error": "content is required"}

    try:
        resolved = validate_path(file_path, settings.workspace_path)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    # Create parent directories if needed
    resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Atomic write: temp file + rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(resolved.parent), suffix=".tmp", prefix=".write_"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, resolved)
    except Exception as e:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return {"success": False, "error": f"Failed to write file: {e}"}

    return {
        "success": True,
        "file_path": file_path,
        "bytes_written": len(content.encode("utf-8")),
        "message": f"File written: {file_path}",
    }


# ---------------------------------------------------------------------------
# Schemas & Registration
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a text file from the agent workspace. "
            "Returns the file content, size, and hash. "
            "Use offset and limit to read specific sections of large files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to workspace or absolute within workspace)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (0-based, default 0)",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read (0 = read all, up to limit)",
                    "default": 0,
                },
            },
            "required": ["file_path"],
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write text content to a file in the agent workspace. "
            "Creates the file if it doesn't exist, overwrites if it does. "
            "Parent directories are created automatically. "
            "Uses atomic write (temp file + rename) for safety."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to workspace or absolute within workspace)",
                },
                "content": {
                    "type": "string",
                    "description": "The text content to write",
                },
            },
            "required": ["file_path", "content"],
        },
    },
}

registry.register(
    name="read_file",
    schema=READ_FILE_SCHEMA,
    handler=_read_file,
    category="files",
    enabled_by_default=True,
    description="Read a file from the agent workspace.",
)

registry.register(
    name="write_file",
    schema=WRITE_FILE_SCHEMA,
    handler=_write_file,
    category="files",
    enabled_by_default=True,
    description="Write a file to the agent workspace.",
)
