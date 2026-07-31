"""Task 2: regression baseline for the pptx SKILL.md section list.

After the frontmatter fix, we want to guarantee that future content additions
(Quality overhaul tasks 3-9) do not drop or rename the existing sections by
accident. This test pins the current `##` section list.

If you intentionally add/remove/rename a section, update `EXPECTED_SECTIONS`
in the SAME commit.
"""
from pathlib import Path
import re

PPTX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pptx" / "SKILL.md"

EXPECTED_SECTIONS = [
    "## Quick Reference",
    "## Reading Content",
    "## Editing Workflow",
    "## Creating from Scratch",
    "## Design Ideas",
    "## QA (Required)",
    "## Converting to Images",
    "## Dependencies",
    "## Slide-type Conventions",
]


def _read() -> str:
    return PPTX_SKILL.read_text(encoding="utf-8")


def _section_headers(text: str):
    return re.findall(r"^## .+$", text, re.MULTILINE)


def test_pptx_skill_section_list_exact():
    """Pin the current section list so additions in upcoming tasks are
    visible (added) but no existing section is silently dropped or renamed."""
    headers = _section_headers(_read())
    assert headers == EXPECTED_SECTIONS, (
        "pptx SKILL.md ## section list regressed.\n"
        f"  expected: {EXPECTED_SECTIONS}\n"
        f"  actual:   {headers}\n"
        "If you intentionally changed a section, update EXPECTED_SECTIONS."
    )


def test_pptx_skill_has_h1():
    text = _read()
    m = re.match(r"^---\n.*?\n---\n+# .+$", text, re.MULTILINE)
    assert m is not None, "pptx SKILL.md missing H1 after frontmatter"


def test_pptx_skill_section_count_at_least_nine():
    """Guards against accidental section drops. We expect 9 baseline
    sections before the overhaul adds more."""
    text = _read()
    headers = _section_headers(text)
    assert len(headers) >= len(EXPECTED_SECTIONS), (
        f"pptx SKILL.md has only {len(headers)} ## sections, expected >= {len(EXPECTED_SECTIONS)}"
    )


def test_pptx_skill_design_ideas_still_has_palettes_table():
    """The Design Ideas section is large and table-heavy. Lock in the palette
    table header so the section can't be silently gutted."""
    text = _read()
    assert "### Color Palettes" in text, "Design Ideas must still contain ### Color Palettes"
    assert "**Midnight Executive**" in text, "Design Ideas must still name the Midnight Executive palette"
    assert "**Forest & Moss**" in text, "Design Ideas must still name the Forest & Moss palette"


def test_pptx_skill_qa_uses_subagent_prompt():
    text = _read()
    assert "**⚠️ USE SUBAGENTS**" in text, "QA section must still recommend subagent visual review"
    assert "Visually inspect these slides" in text, "QA section must still ship the subagent prompt"
