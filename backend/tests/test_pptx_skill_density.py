"""Task 6: pptx SKILL.md must contain a Content Density section with
the 6x6 rule, a per-slide-type word budget table, white-space rules,
anti-patterns, and a density sanity check script.
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


def test_density_section_exists():
    text = _read()
    assert re.search(r"^## Content Density$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Content Density`"
    )


def test_density_section_states_six_by_six_rule():
    body = _section("Content Density").lower()
    # The 6x6 rule is the named ceiling
    assert "6" in body and "rule" in body, "Density section must state the 6x6 rule"


def test_density_section_has_per_type_budget_table():
    body = _section("Content Density")
    # Markdown table with at least Cover, Content, Comparison, Summary rows
    for label in ["Cover", "Section divider", "Content", "Comparison", "Summary"]:
        assert label in body, f"Per-type budget table must include a {label!r} row"


def test_density_section_has_white_space_rules():
    body = _section("Content Density").lower()
    assert "white space" in body or "white-space" in body, (
        "Density section must include a White-Space Rules subsection"
    )
    assert "0.5" in body, "White-space rules must mention the 0.5\" margin minimum"
    assert "0.3" in body, "White-space rules must mention the 0.3\" gap minimum"


def test_density_section_has_anti_patterns():
    body = _section("Content Density").lower()
    assert "anti-pattern" in body or "antipattern" in body, (
        "Density section must list density anti-patterns"
    )


def test_density_section_has_sanity_check_script():
    body = _section("Content Density")
    assert "python-pptx" in body or "Presentation(" in body, (
        "Density section must include a python-pptx sanity-check script"
    )
    assert "body_words" in body or "word" in body, (
        "Sanity-check script must reference word counts"
    )


def test_density_section_forbids_wall_of_bullets():
    body = _section("Content Density").lower()
    assert "8 bullets" in body or "wall" in body or "cramming" in body, (
        "Density section must forbid cramming 8+ bullets on a slide"
    )
