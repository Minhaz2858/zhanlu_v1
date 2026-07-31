"""Task 5: pptx SKILL.md must contain an Anti-Patterns Gallery with
exactly 8 patterns, each in its own `### N.` subsection, with the
failure mode, why it's bad, and a fix.
"""
from pathlib import Path
import re

PPTX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pptx" / "SKILL.md"


def _read() -> str:
    return PPTX_SKILL.read_text(encoding="utf-8")


def _section(name: str) -> str:
    text = _read()
    m = re.search(rf"^## {re.escape(name)}$.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


def test_antipatterns_section_exists():
    text = _read()
    assert re.search(r"^## Anti-Patterns Gallery$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Anti-Patterns Gallery`"
    )


def test_antipatterns_has_eight_entries():
    body = _section("Anti-Patterns Gallery")
    assert body, "Anti-Patterns Gallery section not isolated"
    entries = re.findall(r"^### \d+\.\s+", body, re.MULTILINE)
    assert len(entries) == 8, (
        f"Anti-Patterns Gallery must have exactly 8 patterns, found {len(entries)}: {entries}"
    )


def test_antipatterns_entries_have_fix():
    body = _section("Anti-Patterns Gallery")
    assert body, "section not isolated"
    # Each entry should have a `**Fix:**` line
    fixes = re.findall(r"\*\*Fix:\*\*", body)
    assert len(fixes) >= 8, (
        f"each of 8 anti-patterns must have a `**Fix:**` line, found {len(fixes)}"
    )


def test_antipatterns_includes_accent_line():
    body = _section("Anti-Patterns Gallery").lower()
    assert "accent line" in body, "Anti-Patterns Gallery must include the accent-line pattern"


def test_antipatterns_includes_wall_of_bullets():
    body = _section("Anti-Patterns Gallery").lower()
    assert "wall of bullets" in body or "wall-of-bullets" in body, (
        "Anti-Patterns Gallery must include the wall-of-bullets pattern"
    )


def test_antipatterns_includes_no_summary():
    body = _section("Anti-Patterns Gallery").lower()
    assert "no summary" in body or "no synthesis" in body, (
        "Anti-Patterns Gallery must include the no-summary pattern"
    )


def test_antipatterns_sits_between_qa_and_images():
    text = _read()
    qa = re.search(r"^## QA \(Required\)$", text, re.MULTILINE)
    ap = re.search(r"^## Anti-Patterns Gallery$", text, re.MULTILINE)
    im = re.search(r"^## Converting to Images$", text, re.MULTILINE)
    assert qa and ap and im, "section markers missing"
    assert qa.start() < ap.start() < im.start(), (
        f"Anti-Patterns Gallery must sit between QA (offset {qa.start()}) and Converting to Images (offset {im.start()})"
    )
