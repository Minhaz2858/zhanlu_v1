"""Task 7: pptx SKILL.md must contain a Color & Contrast section with
the 60-30-10 dominance rule, WCAG ratio scoring, pre-validated
foreground/background pairs, a list of forbidden combinations, and
a contrast-sanity-check script.
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


def test_contrast_section_exists():
    text = _read()
    assert re.search(r"^## Color & Contrast$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Color & Contrast`"
    )


def test_contrast_states_60_30_10_rule():
    body = _section("Color & Contrast")
    assert "60-30-10" in body or "60/30/10" in body or "60-70%" in body, (
        "Color & Contrast section must state the 60-30-10 dominance rule"
    )


def test_contrast_section_states_wcag_aa_threshold():
    body = _section("Color & Contrast")
    assert "4.5" in body, "Color & Contrast must state WCAG 2.1 AA body-text threshold (4.5:1)"
    assert "3.0" in body or "3:1" in body, "Color & Contrast must state WCAG 2.1 AA large-text threshold (3.0:1)"


def test_contrast_section_has_pre_validated_combo_table():
    body = _section("Color & Contrast")
    # Need a markdown table with Foreground / Background columns and a Ratio column
    assert "Foreground" in body, "Pre-validated table must have a Foreground column"
    assert "Background" in body, "Pre-validated table must have a Background column"
    assert "Ratio" in body, "Pre-validated table must have a Ratio column"
    # At least 5 hex codes (rough proxy for actual combos)
    hex_codes = re.findall(r"#[0-9A-Fa-f]{6}", body)
    assert len(hex_codes) >= 5, (
        f"Pre-validated combo table must include ≥5 hex codes, found {len(hex_codes)}"
    )


def test_contrast_section_lists_forbidden_combos():
    body = _section("Color & Contrast").lower()
    assert "never" in body or "do not" in body, (
        "Color & Contrast section must list forbidden combinations"
    )
    # At least one specific call-out
    forbidden_substrings = ["light gray", "yellow", "pastel pink", "gradient"]
    matches = [s for s in forbidden_substrings if s in body]
    assert len(matches) >= 2, (
        f"Forbidden combinations list must include ≥2 specific call-outs, found: {matches}"
    )


def test_contrast_section_has_sanity_check_script():
    body = _section("Color & Contrast")
    # The script should compute relative luminance and a contrast ratio
    assert "relative_luminance" in body or "luminance" in body, (
        "Sanity-check script must include relative luminance computation"
    )
    assert "contrast_ratio" in body or "contrast" in body.lower(), (
        "Sanity-check script must compute a contrast ratio"
    )


def test_contrast_section_mentions_color_blindness():
    body = _section("Color & Contrast").lower()
    assert "color-blind" in body or "colorblind" in body or "deuteranopia" in body or "protanopia" in body, (
        "Color & Contrast section must address color-blindness safety"
    )


def test_contrast_section_sits_between_density_and_audit():
    text = _read()
    density = re.search(r"^## Content Density$", text, re.MULTILINE)
    contrast = re.search(r"^## Color & Contrast$", text, re.MULTILINE)
    audit = re.search(r"^## Pre-Emit Self-Audit", text, re.MULTILINE)
    assert density and contrast and audit, "section markers missing"
    assert density.start() < contrast.start() < audit.start(), (
        f"Color & Contrast must sit between Content Density and Pre-Emit Self-Audit"
    )
