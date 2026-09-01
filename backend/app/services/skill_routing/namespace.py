"""Namespaced skill name parsing and collision resolution.

Fully-qualified names use the format ``source:name`` (e.g. ``user:my-ppt``,
``builtin:pptx``).  Bare names (no colon) are resolved by preference tier:

    user > marketplace > generated > builtin / bundled
"""

from __future__ import annotations

from typing import Optional

# ── source-tier priority: lower number = higher preference ──────────────
SOURCE_TIERS: dict[str, int] = {
    "user": 0,
    "marketplace": 1,
    "generated": 2,
    "builtin": 3,
    "bundled": 3,
}


def parse_command(command: str) -> tuple[Optional[str], str]:
    """Parse a (possibly) namespaced skill command.

    Returns ``(source, name)``.  *source* is ``None`` when the command is
    a bare (non-qualified) name.

    >>> parse_command("user:my-ppt")
    ('user', 'my-ppt')
    >>> parse_command("builtin:pptx")
    ('builtin', 'pptx')
    >>> parse_command("pptx")
    (None, 'pptx')
    >>> parse_command("a:b:c")
    (None, 'a:b:c')
    """
    parts = command.split(":", 1)
    if len(parts) == 2 and parts[0] in SOURCE_TIERS:
        return parts[0], parts[1]
    return None, command


def to_namespaced(source: str, name: str) -> str:
    """Build a fully-qualified ``source:name`` string."""
    return f"{source}:{name}"


def resolve_collision(name: str, candidates: list[dict]) -> Optional[dict]:
    """Resolve a bare-name collision among multiple candidates.

    When two skills share the same bare name (e.g. a user-uploaded ``pptx``
    and the built-in ``pptx``), the source tier with the highest preference
    wins: **user > marketplace > generated > builtin > bundled**.

    Returns the winning candidate dict (which must contain at least
    ``"source"``), or ``None`` if *candidates* is empty.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Sort by source-tier priority (lower tier number = higher preference),
    # then by priority field within same tier (higher number = higher pref).
    def _sort_key(c: dict) -> tuple:
        tier = SOURCE_TIERS.get(c.get("source", "builtin"), 99)
        prio = -(c.get("priority") or 0)  # negative so higher priority sorts first
        return (tier, prio)

    return sorted(candidates, key=_sort_key)[0]
