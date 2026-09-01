"""Tests for the deterministic skill safety scanner."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure backend/ is on sys.path so `app.` imports resolve
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.skill_scanner.scanner import (
    DEFAULT_RULES,
    Finding,
    ScanResult,
    ScanRule,
    SkillScanner,
    scan_text,
)


def test_scan_detects_private_key_pem():
    body = "Here is a key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "private_key_pem" for f in result.findings)


def test_scan_detects_aws_access_key():
    body = "aws_key = AKIAIOSFODNN7EXAMPLE"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "aws_access_key" for f in result.findings)


def test_scan_detects_github_token():
    body = "token = ghp_1234567890abcdefghijklmnopqrstuvwxyz"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "github_token" for f in result.findings)


def test_scan_detects_shell_exec():
    body = 'os.system("rm -rf /tmp/test")'
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "shell_exec" for f in result.findings)


def test_scan_detects_subprocess_run():
    body = "subprocess.run(['ls', '-la'])"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "shell_exec" for f in result.findings)


def test_scan_detects_eval():
    body = "result = eval(user_input)"
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is True
    assert any(f.rule_id == "eval_exec" for f in result.findings)


def test_scan_no_false_positive_on_benign_text():
    body = (
        "# My Skill\n\nThis skill helps with documentation.\n"
        "It does not execute any code.\n\n## Usage\n\n"
        "Just read the instructions."
    )
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is False
    assert len(result.findings) == 0


def test_scan_no_false_positive_on_documentation_mentions():
    body = "This skill mentions os.system and subprocess in its docs but does not call them."
    result = scan_text(body, skill_name="test-skill")
    assert result.has_critical is False


def test_secret_redaction_in_matched_text():
    body = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKjQ4w==\n-----END RSA PRIVATE KEY-----"
    result = scan_text(body, skill_name="test-skill")
    key_findings = [f for f in result.findings if f.rule_id == "private_key_pem"]
    assert len(key_findings) == 1
    assert "MIIBOgIBAAJBAKjQ4w" not in key_findings[0].matched_text
    assert "REDACTED" in key_findings[0].matched_text


def test_finding_has_line_number():
    body = "line one\nline two\nos.system('ls')\nline four"
    result = scan_text(body, skill_name="test-skill")
    shell_findings = [f for f in result.findings if f.rule_id == "shell_exec"]
    assert len(shell_findings) == 1
    assert shell_findings[0].line_number == 3


def test_scan_result_summary():
    body = "os.system('ls')\nAKIAIOSFODNN7EXAMPLE"
    result = scan_text(body, skill_name="test-skill")
    assert "2 critical" in result.summary


def test_scan_empty_body():
    result = scan_text("", skill_name="empty-skill")
    assert result.has_critical is False
    assert len(result.findings) == 0
    assert "0 critical" in result.summary


def test_default_rules_count():
    """MVP has exactly 5 critical rules."""
    critical_rules = [r for r in DEFAULT_RULES if r.severity == "critical"]
    assert len(critical_rules) == 5


def test_scanner_with_custom_rules():
    custom = [ScanRule(
        id="custom",
        pattern=__import__("re").compile(r"SECRET_CODE"),
        severity="warning",
        description="Custom rule",
    )]
    scanner = SkillScanner(rules=custom)
    result = scanner.scan(body="SECRET_CODE here", skill_name="test")
    assert len(result.findings) == 1
    assert result.findings[0].rule_id == "custom"
    assert result.has_critical is False


# ── Integration: scan_skill entry point ──────────────────────────────

from app.services.skill_scanner import scan_skill
from app.services.skills_loader import SkillMetadata


def _make_metadata(name="test", body=""):
    return SkillMetadata(
        name=name, description="test", file_path="/tmp/test.md", body=body,
    )


def test_scan_skill_attaches_findings():
    meta = _make_metadata(body="os.system('ls')")
    result = scan_skill(meta)
    assert result.has_critical is True
    assert len(result.findings) == 1


def test_scan_skill_clean_metadata():
    meta = _make_metadata(body="Just a harmless skill.")
    result = scan_skill(meta)
    assert result.has_critical is False
    assert len(result.findings) == 0


def test_scan_skill_disabled_returns_empty(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SKILL_SCAN_ENABLED", False)
    meta = _make_metadata(body="os.system('ls')")
    result = scan_skill(meta)
    assert len(result.findings) == 0
    assert result.has_critical is False


def test_parse_skill_file_attaches_scan_findings(tmp_path, monkeypatch):
    """Integration: parse_skill_file should attach scan_findings to metadata."""
    from app.config import settings
    monkeypatch.setattr(settings, "SKILL_SCAN_ENABLED", True)

    skill_file = tmp_path / "dangerous.md"
    skill_file.write_text(
        "---\nname: dangerous\ndescription: bad skill\n---\n\n"
        "os.system('rm -rf /')\n",
        encoding="utf-8",
    )

    from app.services.skills_loader import parse_skill_file
    meta = parse_skill_file(skill_file, source="test")
    assert meta is not None
    assert len(meta.scan_findings) > 0
    assert meta.scan_findings[0]["severity"] == "critical"


def test_parse_skill_file_clean_skill_has_empty_findings(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SKILL_SCAN_ENABLED", True)

    skill_file = tmp_path / "safe.md"
    skill_file.write_text(
        "---\nname: safe\ndescription: good skill\n---\n\n# Safe Skill\n\nDoes nothing dangerous.\n",
        encoding="utf-8",
    )

    from app.services.skills_loader import parse_skill_file
    meta = parse_skill_file(skill_file, source="test")
    assert meta is not None
    assert meta.scan_findings == []
