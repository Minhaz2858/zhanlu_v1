"""Task 4: pptx SKILL.md must contain a 12-point pre-emit self-audit,
and a long-form rubric at references/quality-checklist.md.

The self-audit is the agent's last stop before emitting the ◤PPTX◤ marker.
The long-form file is the deep-dive for items the agent keeps failing.
"""
from pathlib import Path
import re

PPTX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pptx" / "SKILL.md"
PPTX_REF = (
    Path(__file__).resolve().parents[1]
    / "skills" / "pptx" / "references" / "quality-checklist.md"
)


def test_self_audit_section_exists():
    text = PPTX_SKILL.read_text(encoding="utf-8")
    assert re.search(r"^## Pre-Emit Self-Audit", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Pre-Emit Self-Audit`"
    )


def test_self_audit_lists_exactly_12_points():
    text = PPTX_SKILL.read_text(encoding="utf-8")
    m = re.search(
        r"^## Pre-Emit Self-Audit.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "section not isolated"
    body = m.group(0)
    # Each point is a numbered list item starting with `1.` through `12.`
    points = re.findall(r"^\s*(\d+)\.\s+", body, re.MULTILINE)
    # Filter to those 1..12 in the audit; the section may have intro bullets too
    in_range = [int(p) for p in points if p.isdigit() and 1 <= int(p) <= 12]
    assert sorted(set(in_range)) == list(range(1, 13)), (
        f"Self-Audit must list exactly 12 numbered points. Found: {sorted(set(in_range))}"
    )


def test_self_audit_covers_required_categories():
    text = PPTX_SKILL.read_text(encoding="utf-8")
    m = re.search(
        r"^## Pre-Emit Self-Audit.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(0).lower()
    # Must hit each of the 3 categories (Content, Visual, Polish)
    assert "content" in body, "Self-Audit must have a 'Content' category"
    assert "visual" in body, "Self-Audit must have a 'Visual' category"
    assert "polish" in body, "Self-Audit must have a 'Polish' category"


def test_self_audit_forbids_accent_line():
    text = PPTX_SKILL.read_text(encoding="utf-8")
    m = re.search(
        r"^## Pre-Emit Self-Audit.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(0)
    assert "accent line" in body.lower(), (
        "Self-Audit must explicitly call out the 'accent line under title' AI-slides tell"
    )


def test_self_audit_references_long_form_file():
    text = PPTX_SKILL.read_text(encoding="utf-8")
    m = re.search(
        r"^## Pre-Emit Self-Audit.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(0)
    assert "quality-checklist.md" in body, (
        "Self-Audit section must link to references/quality-checklist.md"
    )


def test_long_form_file_exists():
    assert PPTX_REF.is_file(), f"missing: {PPTX_REF}"


def test_long_form_file_has_all_12_sections():
    text = PPTX_REF.read_text(encoding="utf-8")
    # The long-form should have a section for each of the 12 points
    missing = []
    for i in range(1, 13):
        pattern = rf"^## {i}\.\s+"
        if not re.search(pattern, text, re.MULTILINE):
            missing.append(i)
    assert not missing, f"Long-form file missing sections for points: {missing}"


def test_long_form_file_mentions_wcag():
    text = PPTX_REF.read_text(encoding="utf-8")
    assert "WCAG" in text, "Long-form rubric must reference WCAG 2.1 AA contrast standard"


def test_long_form_file_has_contrast_table():
    text = PPTX_REF.read_text(encoding="utf-8")
    # Need a markdown table with hex foreground/background
    assert "|" in text and "Foreground" in text and "Background" in text, (
        "Long-form rubric must include a pre-validated contrast table"
    )
