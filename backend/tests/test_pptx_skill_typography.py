"""Task 8: pptx SKILL.md must contain a Typography Hierarchy section
with the 4-level type system, hierarchy rules, paragraph rhythm,
a sanity-check script, and anti-patterns.
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


def test_typography_section_exists():
    text = _read()
    assert re.search(r"^## Typography Hierarchy$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Typography Hierarchy`"
    )


def test_typography_states_four_level_system():
    body = _section("Typography Hierarchy")
    assert "4-Level" in body or "four-level" in body or "4 level" in body.lower(), (
        "Typography section must reference the 4-level type system"
    )
    # Each level must be named (H1, H2, H3, Body, Caption at minimum)
    for level in ["H1", "H2", "H3", "Body", "Caption"]:
        assert level in body, f"4-level system must name {level!r}"


def test_typography_table_has_size_weight_letter_spacing():
    body = _section("Typography Hierarchy")
    for col in ["Size", "Weight", "Letter-spacing"]:
        assert col in body, f"Type-system table must have a {col!r} column"


def test_typography_section_forbids_centered_body():
    body = _section("Typography Hierarchy").lower()
    assert "left-align" in body or "left align" in body, (
        "Typography section must recommend left-aligned body"
    )
    # And explicitly call out the centered-body AI tell
    assert "center" in body, "Typography section must call out centered body as anti-pattern"


def test_typography_section_has_rhythm_rules():
    body = _section("Typography Hierarchy")
    assert "1.2" in body and "1.4" in body, "Rhythm rules must state 1.2-1.4 line-spacing range"


def test_typography_section_has_sanity_check_script():
    body = _section("Typography Hierarchy")
    assert "python-pptx" in body or "Presentation(" in body, (
        "Typography section must include a python-pptx sanity-check script"
    )
    assert "fonts" in body.lower() and "sizes" in body.lower(), (
        "Sanity-check script must check fonts and sizes"
    )


def test_typography_section_has_anti_patterns():
    body = _section("Typography Hierarchy").lower()
    assert "anti-pattern" in body or "antipattern" in body, (
        "Typography section must list anti-patterns"
    )


def test_typography_section_sits_between_contrast_and_audit():
    text = _read()
    contrast = re.search(r"^## Color & Contrast$", text, re.MULTILINE)
    typo = re.search(r"^## Typography Hierarchy$", text, re.MULTILINE)
    audit = re.search(r"^## Pre-Emit Self-Audit", text, re.MULTILINE)
    assert contrast and typo and audit, "section markers missing"
    assert contrast.start() < typo.start() < audit.start(), (
        f"Typography Hierarchy must sit between Color & Contrast and Pre-Emit Self-Audit"
    )
