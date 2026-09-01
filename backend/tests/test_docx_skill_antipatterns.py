"""Task 13: docx SKILL.md must contain an Anti-Patterns Gallery with
exactly 8 patterns, each in its own `### N.` subsection.
"""
from pathlib import Path
import re

DOCX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "docx" / "SKILL.md"


def _read() -> str:
    return DOCX_SKILL.read_text(encoding="utf-8")


def _section(name: str) -> str:
    text = _read()
    m = re.search(rf"^## {re.escape(name)}$.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


def test_antipatterns_section_exists():
    text = _read()
    assert re.search(r"^## Anti-Patterns Gallery$", text, re.MULTILINE), (
        "docx SKILL.md must contain `## Anti-Patterns Gallery`"
    )


def test_antipatterns_has_eight_entries():
    body = _section("Anti-Patterns Gallery")
    assert body, "section not isolated"
    entries = re.findall(r"^### \d+\.\s+", body, re.MULTILINE)
    assert len(entries) == 8, (
        f"Anti-Patterns Gallery must have exactly 8 patterns, found {len(entries)}: {entries}"
    )


def test_antipatterns_entries_have_fix():
    body = _section("Anti-Patterns Gallery")
    fixes = re.findall(r"\*\*Fix:\*\*", body)
    assert len(fixes) >= 8, (
        f"each of 8 anti-patterns must have a `**Fix:**` line, found {len(fixes)}"
    )


def test_antipatterns_includes_wall_of_text():
    body = _section("Anti-Patterns Gallery").lower()
    assert "wall of text" in body, "Anti-Patterns Gallery must include the wall-of-text pattern"


def test_antipatterns_includes_wrong_doc_type():
    body = _section("Anti-Patterns Gallery").lower()
    assert "wrong document type" in body or "wrong type" in body, (
        "Anti-Patterns Gallery must include the wrong-document-type pattern"
    )


def test_antipatterns_includes_justified_text():
    body = _section("Anti-Patterns Gallery").lower()
    assert "justified" in body, "Anti-Patterns Gallery must call out justified body text"


def test_antipatterns_sits_between_audit_and_creating():
    text = _read()
    audit = re.search(r"^## Pre-Emit Self-Audit", text, re.MULTILINE)
    ap = re.search(r"^## Anti-Patterns Gallery$", text, re.MULTILINE)
    cnd = re.search(r"^## Creating New Documents$", text, re.MULTILINE)
    assert audit and ap and cnd, "section markers missing"
    assert audit.start() < ap.start() < cnd.start(), (
        "Anti-Patterns Gallery must sit between Pre-Emit Self-Audit and Creating New Documents"
    )
