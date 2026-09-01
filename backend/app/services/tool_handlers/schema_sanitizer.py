"""Schema sanitizer — strips/escapes strings that could confuse the model.

Hermes uses this when building dynamic tool schemas or echoing user
content back into prompts. In zhanlu we apply it sparingly (only where we
build schemas at runtime from user input — e.g. delegate_task's dynamic
sub-agent prompt) to keep the existing static schemas untouched.
"""

from __future__ import annotations

import re
from typing import Any

# Code-fence trigger patterns. If a string contains ```, the model may
# interpret it as starting/ending a code block. Escape it by zero-width
# joining. (Hermes uses Unicode private-use area chars; we use a simpler
# approach: replace with a marker that the model can still see but won't
# trigger framing.)
_FENCE_PATTERN = re.compile(r"```+")

# Special control sequences (CDATA tags, framing tokens) that shouldn't leak
# into model context.
_CDATA_TAGS = ("<![CDATA[", "]]>")

# System / role-like token sequences used by some LLM stacks.
_ROLE_TOKENS = re.compile(
    r"(?:<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]|<</SYS>>|<<SYS>>)",
    re.IGNORECASE,
)


def sanitize_schema_text(text: str) -> str:
    """Make a string safe to embed in a tool description.

    - Strips control sequences that could trigger prompt framing.
    - Replaces triple-backtick sequences with a zero-width marker.
    - Removes CDATA tags.
    """
    if not text:
        return text
    text = _ROLE_TOKENS.sub("", text)
    text = _FENCE_PATTERN.sub("`​`​`", text)  # zero-width between backticks
    for tag in _CDATA_TAGS:
        text = text.replace(tag, "")
    return text


def sanitize_schema(schema: Any) -> Any:
    """Recursively sanitize a tool schema's string fields.

    Walks dicts and lists, sanitizing any str value. Non-string leaves pass
    through unchanged.
    """
    if isinstance(schema, str):
        return sanitize_schema_text(schema)
    if isinstance(schema, dict):
        return {k: sanitize_schema(v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [sanitize_schema(v) for v in schema]
    return schema
