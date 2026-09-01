"""Task 12: docx SKILL.md must contain a Pre-Emit Self-Audit (10 points)
section, plus a long-form rubric at references/quality-checklist.md.

The checklist is the agent's last stop before emitting the
◤MD_DOCX◤ or ◤HTML_DOCX◤ marker. The long-form is the deep-dive
for items the agent keeps failing.
"""
from pathlib import Path
import re

DOCX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "docx" / "SKILL.md"
DOCX_REF = (
    Path(__file__).resolve().parents[1]
    / "skills" / "docx" / "references" / "quality-checklist.md"
)


def test_self_audit_section_exists():
    text = DOCX_SKILL.read_text(encoding="utf-8")
    assert re.search(r"^## Pre-Emit Self-Audit", text, re.MULTILINE), (
        "docx SKILL.md must contain `## Pre-Emit Self-Audit`"
    )


def test_self_audit_lists_exactly_10_points():
    text = DOCX_SKILL.read_text(encoding="utf-8")
    m = re.search(
        r"^## Pre-Emit Self-Audit.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "section not isolated"
    body = m.group(0)
    points = re.findall(r"^\s*(\d+)\.\s+\*\*", body, re.MULTILINE)
    in_range = sorted({int(p) for p in points if 1 <= int(p) <= 10})
    assert in_range == list(range(1, 11)), (
        f"Self-Audit must list exactly 10 numbered points. Found: {in_range}"
    )


def test_self_audit_covers_required_categories():
    text = DOCX_SKILL.read_text(encoding="utf-8")
    m = re.search(
        r"^## Pre-Emit Self-Audit.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(0).lower()
    for cat in ["content", "layout", "quality"]:
        assert cat in body, f"Self-Audit must have a {cat!r} category header"


def test_self_audit_references_xsd_validation():
    text = DOCX_SKILL.read_text(encoding="utf-8")
    m = re.search(
        r"^## Pre-Emit Self-Audit.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(0)
    assert "XSD" in body or "xsd" in body or "valid" in body.lower(), (
        "Self-Audit must reference XSD validation (item 9)"
    )


def test_self_audit_references_long_form_file():
    text = DOCX_SKILL.read_text(encoding="utf-8")
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
    assert DOCX_REF.is_file(), f"missing: {DOCX_REF}"


def test_long_form_file_has_all_10_sections():
    text = DOCX_REF.read_text(encoding="utf-8")
    missing = []
    for i in range(1, 11):
        pattern = rf"^## {i}\.\s+"
        if not re.search(pattern, text, re.MULTILINE):
            missing.append(i)
    assert not missing, f"Long-form file missing sections for points: {missing}"


def test_long_form_file_mentions_libreoffice():
    text = DOCX_REF.read_text(encoding="utf-8")
    assert "libreoffice" in text.lower() or "soffice" in text, (
        "Long-form rubric must mention LibreOffice/soffice for the conversion check"
    )


def test_long_form_file_mentions_xsd():
    text = DOCX_REF.read_text(encoding="utf-8")
    assert "XSD" in text, "Long-form rubric must reference XSD validation"


def test_long_form_file_has_pandoc_footer_recommendation():
    text = DOCX_REF.read_text(encoding="utf-8")
    assert "pageNumber" in text and "footer" in text.lower(), (
        "Long-form rubric must include the pandoc pageNumber footer pattern"
    )
