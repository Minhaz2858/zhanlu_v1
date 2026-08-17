"""Deterministic repair dispatcher for the P0.1 self-healing quality loop.

``repair_artifact(format, data, report)`` takes a semantic-audit report
and applies the mechanically-fixable repairs for the matching format.
Returns the repaired bytes, or ``None`` when there is nothing fixable
(or the format has no repair module).  Never raises.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Only FAIL-level rules are eligible for auto-repair — WARN issues (e.g.
# contrast, alt-text) are advisory and must not be silently rewritten.
_LEVEL_ELIGIBLE = {"FAIL"}


def _failed_rule_ids(report: dict) -> set[str]:
    """Collect the FAIL rule ids from an audit report."""
    if not isinstance(report, dict):
        return set()
    rules = report.get("rules") or []
    ids: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("level") in _LEVEL_ELIGIBLE:
            rid = rule.get("id")
            if rid:
                ids.add(rid)
    return ids


def repair_artifact(format: str, data: bytes, report: dict) -> Optional[bytes]:
    """Apply deterministic repairs for a FAIL audit report.

    Returns repaired bytes, or ``None`` (nothing to do / unknown format).
    """
    if not data:
        return None

    rule_ids = _failed_rule_ids(report)
    if not rule_ids:
        return None

    fmt = (format or "").lower().strip()
    try:
        if fmt == "pptx":
            from app.services.artifacts.repairs.repair_deck import repair_deck

            return repair_deck(data, rule_ids)
        if fmt == "docx":
            from app.services.artifacts.repairs.repair_doc import repair_doc

            return repair_doc(data, rule_ids)
    except Exception as e:  # noqa: BLE001 — repair is best-effort
        logger.warning("repair_artifact(%s) failed: %s", fmt, e)

    return None
