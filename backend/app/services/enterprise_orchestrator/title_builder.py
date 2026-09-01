"""Enterprise title builder.

Design spec §12: enterprise report titles follow the pattern
``"{period_label} {domain_label} Report"``. They must NEVER be a
generic heading like "Executive Summary" / "Key Metrics" / etc. —
those are PROMPTED by section headers, not valid document titles.

The ``_GENERIC_HEADING_BLOCKLIST`` is shared with
``generation_orchestrator._title_from_prose`` (Phase 1C wires both
modules to the same blocklist so they stay in sync).
"""
from __future__ import annotations

from typing import Iterable

#: Headings that look like prose section titles but are NEVER a valid
#: report document title (lowercase, normalized).
_GENERIC_HEADING_BLOCKLIST: frozenset[str] = frozenset({
    "executive summary",
    "key metrics",
    "breakdown by dimension",
    "anomalies & risks",
    "recommended actions",
    "appendix note",
    "operational drivers",
    "primary metric breakdown",
    "segment decomposition",
    "risk assessment",
    "supply-demand drivers",
    "transmission risk",
    "operational drivers & anomalies",
    "actionable recommendations",
})


def sanitize_title(text: str | None) -> str:
    """Return ``text`` unchanged unless it consists ENTIRELY of a
    blocklisted generic heading. If so, return empty string so
    callers can fall through to a real period/domain label."""
    if not text or not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if cleaned.lower() in _GENERIC_HEADING_BLOCKLIST:
        return ""
    return cleaned


def build_enterprise_title(
    *,
    period_label: str | None,
    domain_label: str | None,
    fallback: str = "Executive Report",
) -> str:
    """Build the enterprise report document title.

    Pattern: ``"{period_label} {domain_label} Report"`` with the
    fallback applied only when BOTH components are missing/blocklisted.
    """
    period = sanitize_title(period_label)
    if not period:
        period = ""
    domain = sanitize_title(domain_label)
    if not domain:
        # Domain was blocklisted/missing → default to a neutral label.
        # The user's spec says the title should still tell the user
        # what they're looking at; "Executive" is the safe default.
        domain = "Executive"
    parts = [p for p in (period, domain) if p]
    title = " ".join(parts).strip()
    if not title:
        return sanitize_title(fallback) or "Executive Report"
    title = f"{title} Report"
    return title[:120]


def is_blocklisted(text: str | None) -> bool:
    """True if ``text`` (lowercased, stripped) is in the blocklist."""
    if not text or not isinstance(text, str):
        return True
    return text.strip().lower() in _GENERIC_HEADING_BLOCKLIST


def blocklisted_iter() -> Iterable[str]:
    """Iterate over the blocklist (for diagnostics / debugging)."""
    return tuple(sorted(_GENERIC_HEADING_BLOCKLIST))
