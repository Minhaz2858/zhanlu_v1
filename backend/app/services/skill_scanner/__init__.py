"""Skill safety scanner package — warn-only deterministic scanning."""
from __future__ import annotations

import logging

from app.config import settings
from app.services.skill_scanner.scanner import (
    DEFAULT_RULES,
    Finding,
    ScanResult,
    ScanRule,
    SkillScanner,
    scan_text,
)

logger = logging.getLogger(__name__)

_scanner: SkillScanner | None = None


def _get_scanner() -> SkillScanner:
    global _scanner
    if _scanner is None:
        _scanner = SkillScanner()
    return _scanner


def scan_skill(metadata) -> ScanResult:
    """Scan a parsed SkillMetadata for security issues.

    Warn-only: findings are logged and returned but never block activation.
    Returns an empty ScanResult when SKILL_SCAN_ENABLED is False.
    """
    if not settings.SKILL_SCAN_ENABLED:
        return ScanResult(
            skill_name=getattr(metadata, "name", "unknown"),
            findings=[],
            has_critical=False,
            summary="scan disabled",
        )

    scanner = _get_scanner()
    result = scanner.scan(
        body=getattr(metadata, "body", "") or "",
        skill_name=getattr(metadata, "name", "unknown"),
    )

    # Log findings at their severity level
    for finding in result.findings:
        if finding.severity == "critical":
            logger.critical(
                "SkillScan [%s] CRITICAL: %s (line %s) — %s",
                result.skill_name, finding.rule_id, finding.line_number,
                finding.description,
            )
        elif finding.severity == "warning":
            logger.warning(
                "SkillScan [%s] WARNING: %s — %s",
                result.skill_name, finding.rule_id, finding.description,
            )
        else:
            logger.info(
                "SkillScan [%s] INFO: %s — %s",
                result.skill_name, finding.rule_id, finding.description,
            )

    return result


__all__ = [
    "scan_skill",
    "scan_text",
    "SkillScanner",
    "ScanRule",
    "Finding",
    "ScanResult",
    "DEFAULT_RULES",
]
