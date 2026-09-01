"""Token-budgeted progressive-disclosure skill catalog.

Produces a formatted ``<available_skills>`` block for injection into the
Skill meta-tool description (or system prompt).  Inspired by Claude's
token-budgeted skill catalog pattern:

* Layer A (every turn): name + summary (≤500 chars) + when_to_use
* Layer B (on selection): full SKILL.md body
* Layer C (on demand): bundled scripts/references
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services.skill_routing.namespace import SOURCE_TIERS

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_CHARS = 15_000


def build_catalog(
    skills: list[dict],
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    always_include: Optional[list[str]] = None,
) -> str:
    """Build a progressive-disclosure catalog block (Layer A metadata only).

    Each entry includes name, source, summary, and when-to-use hints.
    Entries are ordered by ``priority`` descending, then by source-tier
    preference (user > marketplace > generated > builtin > bundled).

    ``always_include`` names are force-included FIRST (in the given order),
    regardless of priority/tier — used to guarantee that turn-relevant
    skills (unified_search hits, the resolver's routed skill) survive
    budget truncation. The block is otherwise truncated to fit within
    *budget_chars* (character count, not token count, for simplicity).
    When truncation is needed the least-relevant entries are dropped and
    a warning is logged.

    Parameters
    ----------
    skills:
        List of skill dicts.  Each dict must have at least:
        ``name``, ``source``, ``description``.
        Optional keys: ``summary``, ``priority``, ``tags``.
    budget_chars:
        Maximum total character length of the returned block.
    always_include:
        Optional list of skill names that must appear in the catalog.
        Unknown names are ignored. Order is preserved; any names not in
        ``skills`` are skipped.

    Returns
    -------
    str
        Formatted catalog string ready for injection into a tool
        description or prompt context.
    """
    if not skills:
        return ""

    # ── Resolve force-include names first (turn-relevant skills) ────────
    forced: list[dict] = []
    remaining: list[dict] = []
    seen: set[str] = set()
    if always_include:
        wanted = [n for n in always_include if n]
        by_name = {sk.get("name"): sk for sk in skills}
        for name in wanted:
            sk = by_name.get(name)
            if sk is not None and name not in seen:
                forced.append(sk)
                seen.add(name)
    for sk in skills:
        if sk.get("name") not in seen:
            remaining.append(sk)

    # ── Sort the remainder: higher priority first, then preferred tier ──
    remaining.sort(key=_sort_key)

    # ── Build entries: forced first, then fill with sorted remainder ────
    lines: list[str] = []
    total_chars = 0
    included = 0
    dropped = 0

    ordered = forced + remaining
    for sk in ordered:
        entry = _format_entry(sk)
        if total_chars + len(entry) > budget_chars:
            dropped += 1
            continue
        lines.append(entry)
        total_chars += len(entry)
        included += 1

    if dropped:
        logger.info(
            "Skill catalog budget exhausted: %d included, %d dropped (budget=%d chars, %d forced)",
            included,
            dropped,
            budget_chars,
            len(forced),
        )

    return "\n".join(lines)


# ── helpers ────────────────────────────────────────────────────────────


def _sort_key(skill: dict) -> tuple:
    """Sort key: higher priority first, then preferred source tier."""
    priority = -(skill.get("priority") or 0)  # negative → higher is first
    tier = SOURCE_TIERS.get(skill.get("source", "builtin"), 99)
    return (priority, tier)


def _format_entry(skill: dict) -> str:
    """Format a single skill as a catalog entry (Layer A metadata only)."""
    name: str = skill.get("name", "unknown")
    source: str = skill.get("source", "builtin")
    description: str = _first_line(skill.get("description", ""))
    summary: str = _first_line(skill.get("summary", ""))

    tag_hint = ""
    tags = skill.get("tags")
    if tags and isinstance(tags, list):
        tag_hint = ", tags: " + ", ".join(tags[:5])

    # Use summary if available (shorter), else first-line of description
    detail = summary if summary else description

    lines = [
        f"  <skill>",
        f"    <name>{name}</name>",
        f"    <source>{source}</source>",
    ]
    if detail:
        lines.append(f"    <description>{detail}</description>")
    if tag_hint:
        lines.append(f"    <hint>{tag_hint.strip(', ')}</hint>")
    lines.append(f"  </skill>")
    return "\n".join(lines)


def _first_line(text: str, max_len: int = 500) -> str:
    """Return the first line of *text*, capped to *max_len* chars."""
    if not text:
        return ""
    line = text.split("\n", 1)[0].strip()
    if len(line) > max_len:
        line = line[: max_len - 3] + "..."
    return line
