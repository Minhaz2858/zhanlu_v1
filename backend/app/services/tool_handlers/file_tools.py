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

# Safety cap for scanning the upload root when the agent passes a bare filename.
_MAX_UPLOAD_SCAN = 2000


def _resolve_upload_path(file_path: str) -> Path | None:
    """Resolve an uploaded-file reference to an on-disk path under the upload root.

    Handles three agent-issued shapes (the agent may pass any of them when it
    means a previously uploaded document):
      - ``/api/uploads/<name>``  → direct safe mapping under upload root
      - an absolute/relative path that already resolves under the upload root
      - a bare filename (``Regional_Address_Distribution_Report.docx``) → matched
        against the upload root's basenames (the agent often only knows the name)

    Returns ``None`` when the reference does not clearly point at an uploaded
    file, so the caller can fall back to workspace scoping. Never returns a path
    outside the upload root (path-traversal safe, mirrors
    ``document_ingestion.service._resolve_local_path``).
    """
    upload_root = Path(str(settings.upload_path)).resolve()
    if not upload_root.exists():
        return None

    prefix = "/api/uploads/"
    candidate: Path | None = None
    if file_path.startswith(prefix):
        candidate = upload_root / file_path[len(prefix):]
    else:
        p = Path(file_path)
        if p.is_absolute():
            candidate = p
        elif "/" not in file_path and "\\" not in file_path:
            # Bare filename → search the upload root for a basename match.
            name = file_path.strip()
            if not name:
                return None
            matches = [
                f for f in upload_root.iterdir()
                if f.is_file() and f.name == name
            ][:_MAX_UPLOAD_SCAN]
            if len(matches) == 1:
                return matches[0]
            return None
        else:
            candidate = upload_root / file_path

    try:
        resolved = candidate.resolve()
    except (ValueError, OSError):
        return None
    if resolved != upload_root and upload_root not in resolved.parents:
        return None
    return resolved


def _resolve_kb_file_path(name: str, db: Session | None) -> tuple[Path | None, str | None]:
    """Resolve a bare uploaded-document filename to its on-disk upload path.

    Uploaded KnowledgeBase files are stored under a content hash (e.g.
    ``4d6c0fc0….docx``), so a bare human filename like
    ``Regional_Address_Distribution_Report.docx`` won't match the upload root's
    basenames. Look up a ``file``-kind KnowledgeBase by its display name or its
    original ``file_url`` basename, then map that ``file_url`` to disk via the
    upload root (path-traversal safe).

    Returns ``(resolved_path, kb_file_url)`` so the caller can also extract text
    through the document-ingestion pipeline. ``(None, None)`` when no match.
    """
    if not db:
        return (None, None)
    name = name.strip()
    if not name:
        return (None, None)
    # The agent usually passes the original filename (with extension), but the
    # KnowledgeBase ``name`` may omit the extension. Compare both ways.
    name_stem = name
    if "." in name:
        name_stem = name.rsplit(".", 1)[0]
    try:
        from app.models import KnowledgeBase
    except Exception:  # pragma: no cover - model import safety
        return (None, None)
    candidates = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.source_kind == "file")
        .all()
    )
    upload_root = Path(str(settings.upload_path)).resolve()
    for kb in candidates:
        kb_file_url = (kb.file_url or "").strip()
        if not kb_file_url:
            continue
        matches_name = kb.name == name or kb.name == name_stem
        matches_basename = kb_file_url.rstrip("/").endswith("/" + name) or \
            kb_file_url == name
        if not (matches_name or matches_basename):
            continue
        # Map the KB's /api/uploads/<hash>.<ext> to disk, safely.
        if kb_file_url.startswith("/api/uploads/"):
            candidate = upload_root / kb_file_url[len("/api/uploads/"):]
        else:
            candidate = Path(kb_file_url)
            if not candidate.is_absolute():
                candidate = upload_root / kb_file_url
        try:
            resolved = candidate.resolve()
        except (ValueError, OSError):
            continue
        if resolved != upload_root and upload_root not in resolved.parents:
            continue
        if resolved.is_file():
            return (resolved, kb_file_url)
    return (None, None)


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

    # Uploaded documents live under /api/uploads, which is OUTSIDE the agent
    # workspace. If the agent references an uploaded file (by URL or bare
    # filename), resolve it there first; otherwise fall back to workspace scoping.
    kb_file_url: str | None = None
    resolved = _resolve_upload_path(file_path)
    if resolved is None and "/" not in file_path and "\\" not in file_path:
        # Bare filename that didn't match the upload root directly — it may be
        # an uploaded KnowledgeBase document (stored under a content hash, so the
        # basename won't match). Look it up by KB name / original file name.
        resolved, kb_file_url = _resolve_kb_file_path(file_path, db)
    if resolved is None:
        try:
            resolved = validate_path(file_path, settings.workspace_path)
        except ValueError as e:
            return {"success": False, "error": str(e)}

    if not resolved.exists():
        return {"success": False, "error": f"File not found: {file_path}"}
    if not resolved.is_file():
        return {"success": False, "error": f"Not a file: {file_path}"}

    # Uploaded KnowledgeBase documents are binaries (docx/pdf/...). Reading raw
    # bytes would dump zip garbage into the prompt, so extract clean text via the
    # document-ingestion pipeline instead. (Plain workspace text files skip this.)
    if kb_file_url is not None and resolved.suffix.lower() not in (".txt", ".md", ".csv", ".json", ".tsv", ".log"):
        try:
            from app.services.document_ingestion.service import prepare_for_context
            ctx = prepare_for_context(kb_file_url)
            extracted = (ctx.get("text") or "").strip()
            if extracted:
                content = extracted
                file_size = len(extracted.encode("utf-8", "replace"))
                # Apply offset/limit + truncation below using the extracted text.
                _read_raw = False
            else:
                _read_raw = True
        except Exception as e:  # pragma: no cover - fall back to raw bytes
            logger.warning("read_file: text extraction failed for %s: %s", kb_file_url, e)
            _read_raw = True
    else:
        _read_raw = True

    if _read_raw:
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
