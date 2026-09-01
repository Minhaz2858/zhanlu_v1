"""Task 11: docx SKILL.md must contain a Typography & Page Layout section
with the 4-level heading hierarchy, line-spacing rules, page-margin
profiles, page-break discipline, table-width guidance, headers/footers,
anti-patterns, and a sanity-check description.
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


def test_typography_section_exists():
    text = _read()
    assert re.search(r"^## Typography & Page Layout$", text, re.MULTILINE), (
        "docx SKILL.md must contain `## Typography & Page Layout`"
    )


def test_typography_states_four_level_hierarchy():
    body = _section("Typography & Page Layout")
    assert "4-level" in body or "four-level" in body, (
        "Typography section must reference the 4-level heading hierarchy"
    )
    # H1..H4 + body
    for level in ["H1", "H2", "H3", "H4", "Body"]:
        assert level in body, f"4-level hierarchy must name {level!r}"


def test_typography_states_one_h1_per_doc_rule():
    body = _section("Typography & Page Layout")
    assert "one H1" in body.lower() or "1 h1" in body.lower() or "single h1" in body.lower(), (
        "Typography section must state 'one H1 per document' rule"
    )


def test_typography_section_states_line_spacing_range():
    body = _section("Typography & Page Layout")
    assert "1.15" in body and "1.5" in body, "Typography section must state 1.15-1.5 line spacing range"


def test_typography_section_has_margin_profiles():
    body = _section("Typography & Page Layout")
    # Should have a margin table with at least 2 profiles
    for profile in ["Standard", "Letter", "Report"]:
        assert profile in body, f"Margin profiles must include {profile!r}"
    assert "1.0" in body, "Margin profiles must use 1.0\" as a value"


def test_typography_section_addresses_page_breaks():
    body = _section("Typography & Page Layout").lower()
    assert "page break" in body, "Typography section must address page-break discipline"
    assert "widow" in body or "orphan" in body, (
        "Typography section must mention widow/orphan control"
    )


def test_typography_section_has_table_width_rules():
    body = _section("Typography & Page Layout")
    assert "table" in body.lower() and ("width" in body.lower() or "column" in body.lower()), (
        "Typography section must give table-width rules"
    )


def test_typography_section_has_header_footer_section():
    body = _section("Typography & Page Layout").lower()
    assert "header" in body and "footer" in body, (
        "Typography section must cover headers and footers"
    )


def test_typography_section_has_anti_patterns():
    body = _section("Typography & Page Layout").lower()
    assert "anti-pattern" in body or "antipattern" in body, (
        "Typography section must list anti-patterns"
    )
    # Specific call-outs
    matches = sum(1 for w in ["multiple fonts", "all-bold", "centered body", "mixed font"] if w in body)
    assert matches >= 2, f"Typography anti-patterns must include ≥2 specific call-outs, found {matches}"


def test_typography_section_comes_before_creating_new_documents():
    text = _read()
    typo = re.search(r"^## Typography & Page Layout$", text, re.MULTILINE)
    cnd = re.search(r"^## Creating New Documents$", text, re.MULTILINE)
    assert typo and cnd, "section markers missing"
    assert typo.start() < cnd.start(), (
        "Typography & Page Layout must come BEFORE Creating New Documents"
    )
