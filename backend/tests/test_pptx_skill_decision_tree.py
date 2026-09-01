"""Task 3: pptx SKILL.md must contain a 6-question pre-generation decision tree.

The decision tree is the agent's first stop before any code. It must:
- Be a `##` section (not buried in a sub-section)
- Sit before `## Quick Reference` so the agent hits it first
- Cover exactly the 6 dimensions: audience, length, formality, data density,
  brand/template, intent
- Forbid padding / over-building
- Tell the agent to output a 5-line brief it keeps open while building
"""
from pathlib import Path
import re

PPTX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pptx" / "SKILL.md"


def _read() -> str:
    return PPTX_SKILL.read_text(encoding="utf-8")


def _section_offset(name: str) -> int:
    """Return the byte offset of `## name` (or -1 if missing)."""
    text = _read()
    m = re.search(rf"^## {re.escape(name)}$", text, re.MULTILINE)
    return m.start() if m else -1


def test_decision_tree_section_exists():
    text = _read()
    assert re.search(r"^## Pre-Generation Decision Tree$", text, re.MULTILINE), (
        "pptx SKILL.md must contain `## Pre-Generation Decision Tree`"
    )


def test_decision_tree_comes_before_quick_reference():
    """The decision tree must be the agent's FIRST hit after the H1."""
    dt = _section_offset("Pre-Generation Decision Tree")
    qr = _section_offset("Quick Reference")
    assert dt > 0, "decision tree section missing"
    assert qr > 0, "Quick Reference section missing"
    assert dt < qr, (
        f"Decision Tree (offset {dt}) must come BEFORE Quick Reference (offset {qr})"
    )


def test_decision_tree_covers_all_six_dimensions():
    text = _read()
    # Extract just the decision tree section
    m = re.search(
        r"^## Pre-Generation Decision Tree$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "decision tree section not isolated"
    body = m.group(1).lower()

    dimensions = [
        ("audience", "who is reading"),
        ("length", "how many slides"),
        ("formality", "board-formal"),
        ("data density", "numeric content"),
        ("brand", "template or palette"),
        ("intent", "one sentence"),
    ]
    missing = [label for label, hint in dimensions if hint not in body]
    assert not missing, f"decision tree missing dimensions: {missing}"


def test_decision_tree_forbids_padding():
    text = _read()
    m = re.search(
        r"^## Pre-Generation Decision Tree$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(1).lower()
    # Must contain a hard rule against padding
    assert "never pad" in body or "cut" in body, (
        "decision tree must contain a hard rule against padding"
    )


def test_decision_tree_requires_one_sentence_intent():
    text = _read()
    m = re.search(
        r"^## Pre-Generation Decision Tree$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(1)
    assert "one sentence" in body.lower(), (
        "decision tree must require the intent to be captured in one sentence"
    )


def test_decision_tree_prompts_a_5_line_brief():
    text = _read()
    m = re.search(
        r"^## Pre-Generation Decision Tree$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(1)
    assert "5-line brief" in body or "five-line brief" in body, (
        "decision tree must tell the agent to keep a 5-line brief open while building"
    )
