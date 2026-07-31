"""Task 9: pptx SKILL.md must contain an Accessibility Quick Pass section
with 5 minute-level checks, color-blind palette tags, alt-text patterns,
a python-pptx code snippet, and a programmatic check.
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


def test_accessibility_section_exists():
    text = _read()
    assert re.search(r"^## Accessibility Quick Pass$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Accessibility Quick Pass`"
    )


def test_accessibility_has_five_quick_pass_checks():
    body = _section("Accessibility Quick Pass")
    # The five checks should be numbered 1-5
    points = re.findall(r"^\s*(\d+)\.\s+\*\*", body, re.MULTILINE)
    in_range = sorted({int(p) for p in points if 1 <= int(p) <= 5})
    assert in_range == [1, 2, 3, 4, 5], (
        f"Accessibility Quick Pass must have 5 numbered checks. Found: {in_range}"
    )


def test_accessibility_requires_alt_text():
    body = _section("Accessibility Quick Pass").lower()
    assert "alt text" in body or "alt" in body, (
        "Accessibility Quick Pass must require alt text on images"
    )


def test_accessibility_states_body_font_minimum():
    body = _section("Accessibility Quick Pass")
    assert "14pt" in body or "14 pt" in body, (
        "Accessibility Quick Pass must state the 14pt body-font minimum"
    )


def test_accessibility_states_color_signal_rule():
    body = _section("Accessibility Quick Pass").lower()
    assert "color" in body and "signal" in body, (
        "Accessibility Quick Pass must forbid color-only signal"
    )


def test_accessibility_lists_palette_tags():
    body = _section("Accessibility Quick Pass")
    for tag in ["Mono", "Diverging", "Sequential", "Categorical"]:
        assert tag in body, f"Color-blind palette tags must include {tag!r}"


def test_accessibility_has_alt_text_patterns_table():
    body = _section("Accessibility Quick Pass")
    for label in ["Logo", "Chart", "Icon", "Screenshot", "Photo", "Diagram"]:
        assert label in body, f"Alt-text patterns table must include {label!r}"


def test_accessibility_has_python_pptx_code():
    body = _section("Accessibility Quick Pass")
    assert "python-pptx" in body or "Presentation(" in body, (
        "Accessibility section must include python-pptx code for setting alt text"
    )


def test_accessibility_section_sits_between_typography_and_audit():
    text = _read()
    typo = re.search(r"^## Typography Hierarchy$", text, re.MULTILINE)
    a11y = re.search(r"^## Accessibility Quick Pass$", text, re.MULTILINE)
    audit = re.search(r"^## Pre-Emit Self-Audit", text, re.MULTILINE)
    assert typo and a11y and audit, "section markers missing"
    assert typo.start() < a11y.start() < audit.start(), (
        "Accessibility Quick Pass must sit between Typography Hierarchy and Pre-Emit Self-Audit"
    )
