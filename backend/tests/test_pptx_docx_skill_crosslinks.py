"""Task 14: pptx and docx SKILL.md must have reciprocal cross-links
in a 'Related Skills' section, plus a shared 'Professional Output
Principles' section.
"""
from pathlib import Path
import re

PPTX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pptx" / "SKILL.md"
DOCX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "docx" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}$.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


# ---- pptx side ----

def test_pptx_has_related_skills_section():
    text = _read(PPTX_SKILL)
    assert re.search(r"^## Related Skills$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Related Skills`"
    )


def test_pptx_related_skills_links_to_docx():
    body = _section(_read(PPTX_SKILL), "Related Skills")
    assert "docx" in body, "pptx Related Skills must mention the docx skill"
    # The link should be a markdown link to the docx SKILL.md or use the skill name
    assert ("docx/SKILL.md" in body) or ("`docx`" in body), (
        "pptx Related Skills must include a clickable link/reference to the docx skill"
    )


def test_pptx_has_professional_output_principles_section():
    text = _read(PPTX_SKILL)
    assert re.search(r"^## Professional Output Principles$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Professional Output Principles`"
    )


def test_pptx_principles_list_at_least_eight():
    body = _section(_read(PPTX_SKILL), "Professional Output Principles")
    # Should be a numbered list 1-N with 8+ items
    points = re.findall(r"^\s*(\d+)\.\s+\*\*", body, re.MULTILINE)
    n = len({int(p) for p in points if 1 <= int(p) <= 20})
    assert n >= 8, f"pptx principles must list ≥8 numbered points, found {n}"


# ---- docx side ----

def test_docx_has_related_skills_section():
    text = _read(DOCX_SKILL)
    assert re.search(r"^## Related Skills$", text, re.MULTILINE), (
        "docx SKILL.md must contain `## Related Skills`"
    )


def test_docx_related_skills_links_to_pptx():
    body = _section(_read(DOCX_SKILL), "Related Skills")
    assert "pptx" in body, "docx Related Skills must mention the pptx skill"
    assert ("pptx/SKILL.md" in body) or ("`pptx`" in body), (
        "docx Related Skills must include a clickable link/reference to the pptx skill"
    )


def test_docx_has_professional_output_principles_section():
    text = _read(DOCX_SKILL)
    assert re.search(r"^## Professional Output Principles$", text, re.MULTILINE), (
        "docx SKILL.md must contain `## Professional Output Principles`"
    )


def test_docx_principles_list_at_least_eight():
    body = _section(_read(DOCX_SKILL), "Professional Output Principles")
    points = re.findall(r"^\s*(\d+)\.\s+\*\*", body, re.MULTILINE)
    n = len({int(p) for p in points if 1 <= int(p) <= 20})
    assert n >= 8, f"docx principles must list ≥8 numbered points, found {n}"


# ---- cross-file consistency ----

def test_both_skills_have_identical_principle_count():
    pptx_n = len(re.findall(r"^\s*(\d+)\.\s+\*\*", _section(_read(PPTX_SKILL), "Professional Output Principles"), re.MULTILINE))
    docx_n = len(re.findall(r"^\s*(\d+)\.\s+\*\*", _section(_read(DOCX_SKILL), "Professional Output Principles"), re.MULTILINE))
    assert pptx_n == docx_n, (
        f"Both skills must have the same number of principles. pptx={pptx_n}, docx={docx_n}"
    )


def test_both_skills_reference_each_other():
    pptx_text = _read(PPTX_SKILL)
    docx_text = _read(DOCX_SKILL)
    # The docx filename should appear in pptx Related Skills, and pptx in docx Related Skills
    assert "docx" in _section(pptx_text, "Related Skills").lower()
    assert "pptx" in _section(docx_text, "Related Skills").lower()
