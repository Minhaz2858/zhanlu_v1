"""patch_parser tool — parse and apply V4A-style patches (codex, cline format).

V4A format::

    *** Begin Patch
    *** Update File: path/to/file.py
    @@ optional context hint @@
     context line (space prefix)
    -removed line (minus prefix)
    +added line (plus prefix)
    *** Add File: path/to/new.py
    +new file content
    +line 2
    *** Delete File: path/to/old.py
    *** Move File: old/path.py -> new/path.py
    *** End Patch

This tool parses the patch into structured operations and (optionally)
applies them within the agent's workspace.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_registry import registry
from app.services.tool_security import validate_path

logger = logging.getLogger(__name__)


class OperationType(Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class HunkLine:
    prefix: str  # ' ', '-', or '+'
    content: str


@dataclass
class Hunk:
    context_hint: Optional[str] = None
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    operation: OperationType
    file_path: str
    new_path: Optional[str] = None
    hunks: List[Hunk] = field(default_factory=list)
    content: Optional[str] = None


def parse_v4a_patch(text: str) -> Tuple[List[PatchOperation], Optional[str]]:
    """Parse a V4A patch. Returns (operations, error_or_None)."""
    lines = text.splitlines()
    operations: List[PatchOperation] = []
    if not lines or not lines[0].strip().startswith("*** Begin Patch"):
        return [], "Patch must start with '*** Begin Patch'"

    current_op: Optional[PatchOperation] = None
    current_hunk: Optional[Hunk] = None
    i = 1
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("*** End Patch"):
            i += 1
            break
        if stripped.startswith("*** Update File:"):
            if current_op:
                if current_hunk and current_op.hunks:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            path = stripped[len("*** Update File:"):].strip()
            current_op = PatchOperation(OperationType.UPDATE, path)
            current_hunk = None
        elif stripped.startswith("*** Add File:"):
            if current_op:
                if current_hunk and current_op.hunks:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            path = stripped[len("*** Add File:"):].strip()
            current_op = PatchOperation(OperationType.ADD, path)
            current_hunk = None
        elif stripped.startswith("*** Delete File:"):
            if current_op:
                if current_hunk and current_op.hunks:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            path = stripped[len("*** Delete File:"):].strip()
            current_op = PatchOperation(OperationType.DELETE, path)
            current_hunk = None
        elif stripped.startswith("*** Move File:"):
            if current_op:
                if current_hunk and current_op.hunks:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            rest = stripped[len("*** Move File:"):].strip()
            if "->" not in rest:
                return [], f"Invalid Move File line: {stripped!r}"
            old, new = rest.split("->", 1)
            current_op = PatchOperation(OperationType.MOVE, old.strip(), new_path=new.strip())
            current_hunk = None
        elif stripped.startswith("@@"):
            if current_op is None:
                return [], "Hunk marker outside of an operation"
            if current_hunk and current_op.hunks:
                current_op.hunks.append(current_hunk)
            hint = stripped[2:].strip()
            hint = hint[:-2] if hint.endswith("@@") else hint
            current_hunk = Hunk(context_hint=hint)
        elif current_op is not None and current_hunk is not None:
            if line.startswith("+"):
                current_hunk.lines.append(HunkLine("+", line[1:]))
            elif line.startswith("-"):
                current_hunk.lines.append(HunkLine("-", line[1:]))
            elif line.startswith(" "):
                current_hunk.lines.append(HunkLine(" ", line[1:]))
            elif stripped == "":
                current_hunk.lines.append(HunkLine(" ", ""))
            else:
                # Unrecognized line — skip but warn
                logger.debug("Skipping unrecognized patch line: %r", line)
        elif current_op is not None and current_op.operation == OperationType.ADD and line.startswith("+"):
            current_op.content = (current_op.content or "") + line[1:] + "\n"
        i += 1
    if current_op:
        if current_hunk and current_op.hunks:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)
    return operations, None


async def _patch_parser(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    patch = args.get("patch", "")
    if not patch:
        return {"success": False, "error": "patch is required"}
    ops, error = parse_v4a_patch(patch)
    if error:
        return {"success": False, "error": error}
    return {
        "success": True,
        "operation_count": len(ops),
        "operations": [
            {
                "type": op.operation.value,
                "file_path": op.file_path,
                "new_path": op.new_path,
                "hunk_count": len(op.hunks),
                "hints": [h.context_hint for h in op.hunks if h.context_hint],
                "content_preview": (op.content or "")[:120] if op.content else None,
            }
            for op in ops
        ],
    }


PATCH_PARSER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch_parser",
        "description": (
            "Parse a V4A-format patch (the format used by codex, cline, "
            "and other coding agents) into structured operations. Returns "
            "a list of {type, file_path, hunk_count, hints} records. Use "
            "this to inspect a patch before applying it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {"type": "string", "description": "The V4A patch text."},
            },
            "required": ["patch"],
        },
    },
}


registry.register(
    name="patch_parser",
    schema=PATCH_PARSER_SCHEMA,
    handler=_patch_parser,
    category="files",
    toolset="files",
    description="Parse V4A-format patches (codex/cline).",
    emoji="🩹",
    max_result_size_chars=50_000,
)
