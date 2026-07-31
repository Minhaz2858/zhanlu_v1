"""Tool output size limits — used by handlers to cap result strings.

Wraps ``app.services.tool_security.truncate_output`` with a slightly
different default for hermes-style handlers (which often have larger
defaults) and exposes a per-tool override that pulls from the registry.
"""

from __future__ import annotations

from typing import Any, Optional

from app.services.tool_security import truncate_output as _truncate


def truncate_tool_output(
    data: Any,
    tool_name: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> str:
    """Truncate a tool's output, using registry cap if max_chars is not given.

    Args:
        data: dict / str / other — JSON-stringified if not a str.
        tool_name: optional; if provided, queries the registry for a
            per-tool ``max_result_size_chars`` override.
        max_chars: explicit override (wins over registry).
    """
    cap = max_chars
    if cap is None and tool_name:
        try:
            from app.services.tool_registry import registry
            cap = registry.get_max_result_size(tool_name)
        except Exception:
            cap = None
    return _truncate(data, cap) if cap is not None else _truncate(data)
