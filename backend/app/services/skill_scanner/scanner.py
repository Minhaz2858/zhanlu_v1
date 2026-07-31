"""Deterministic skill safety scanner.

Scans skill body text for high-confidence security patterns (private keys,
cloud credentials, shell execution, dynamic code execution). Warn-only —
findings are logged and attached to metadata but never block activation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

Severity = Literal["critical", "warning", "info"]

# Rules whose matched text is a secret — redact before storing/logging.
_SECRET_RULE_IDS: frozenset[str] = frozenset({
    "private_key_pem",
    "aws_access_key",
    "github_token",
})


@dataclass
class ScanRule:
    """A single deterministic scan rule."""
    id: str
    pattern: re.Pattern[str]
    severity: Severity
    description: str


@dataclass
class Finding:
    """A single scan finding."""
    rule_id: str
    severity: Severity
    description: str
    line_number: int | None
    matched_text: str  # truncated to 80 chars, secrets redacted


@dataclass
class ScanResult:
    """Result of scanning a skill."""
    skill_name: str
    findings: list[Finding] = field(default_factory=list)
    has_critical: bool = False
    summary: str = "0 critical, 0 warning, 0 info"


def _redact(matched: str, rule_id: str) -> str:
    """Truncate to 80 chars and redact secrets."""
    if rule_id in _SECRET_RULE_IDS:
        return f"[REDACTED:{rule_id}]"
    return matched[:80]


def _summarize(findings: list[Finding]) -> str:
    critical = sum(1 for f in findings if f.severity == "critical")
    warning = sum(1 for f in findings if f.severity == "warning")
    info = sum(1 for f in findings if f.severity == "info")
    return f"{critical} critical, {warning} warning, {info} info"


# ---------------------------------------------------------------------------
# MVP rules — 5 CRITICAL patterns only (warn-only policy)
# ---------------------------------------------------------------------------

DEFAULT_RULES: list[ScanRule] = [
    ScanRule(
        id="private_key_pem",
        pattern=re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        severity="critical",
        description="Embedded PEM private key detected",
    ),
    ScanRule(
        id="aws_access_key",
        pattern=re.compile(r"AKIA[0-9A-Z]{16}"),
        severity="critical",
        description="AWS access key ID detected",
    ),
    ScanRule(
        id="github_token",
        pattern=re.compile(r"ghp_[a-zA-Z0-9]{36}"),
        severity="critical",
        description="GitHub personal access token detected",
    ),
    ScanRule(
        id="shell_exec",
        pattern=re.compile(r"os\.system\s*\(|os\.popen\s*\(|subprocess\.(call|run|Popen)\s*\("),
        severity="critical",
        description="Shell/process execution detected",
    ),
    ScanRule(
        id="eval_exec",
        pattern=re.compile(r"\beval\s*\(|\bexec\s*\("),
        severity="critical",
        description="Dynamic code execution (eval/exec) detected",
    ),
]


class SkillScanner:
    """Scans skill body text for security patterns."""

    def __init__(self, rules: list[ScanRule] | None = None):
        self.rules = rules if rules is not None else DEFAULT_RULES

    def scan(self, *, body: str, skill_name: str) -> ScanResult:
        findings: list[Finding] = []
        for rule in self.rules:
            for match in rule.pattern.finditer(body):
                line_number = body[:match.start()].count("\n") + 1
                findings.append(Finding(
                    rule_id=rule.id,
                    severity=rule.severity,
                    description=rule.description,
                    line_number=line_number,
                    matched_text=_redact(match.group(0), rule.id),
                ))
        return ScanResult(
            skill_name=skill_name,
            findings=findings,
            has_critical=any(f.severity == "critical" for f in findings),
            summary=_summarize(findings),
        )


def scan_text(body: str, skill_name: str) -> ScanResult:
    """Convenience function: scan a body string with the default scanner."""
    return SkillScanner().scan(body=body, skill_name=skill_name)
