"""Round-trip editors for PPTX/DOCX artifacts (P1.3).

``apply_edits(format, data, operations)`` applies a list of targeted edit
operations to a stored blob and returns ``(edited_bytes, changelog)``.
Edits are atomic: every operation must apply, otherwise ``EditError`` is
raised and the original blob is left untouched.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EditError(Exception):
    """Raised when an edit operation cannot be applied (bad slide index,
    missing ``find`` target, unknown op, ...). The original blob is left
    untouched."""


def apply_edits(format: str, data: bytes, operations: list[dict]) -> tuple[bytes, list[str]]:
    """Apply targeted edits and return ``(edited_bytes, changelog)``.

    ``changelog`` is a list of human-readable entries, one per operation,
    suitable for ``"; ".join(...)``.
    """
    if not data:
        raise EditError("No data to edit")
    if not operations:
        raise EditError("No operations provided")

    fmt = (format or "").lower().strip()
    if fmt == "pptx":
        from app.services.artifacts.editors.pptx_edit import apply_pptx_edits

        return apply_pptx_edits(data, operations)
    if fmt == "docx":
        from app.services.artifacts.editors.docx_edit import apply_docx_edits

        return apply_docx_edits(data, operations)

    raise EditError(f"Unsupported edit format: {format}")
