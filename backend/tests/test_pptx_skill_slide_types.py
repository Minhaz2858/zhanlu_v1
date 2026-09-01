"""Verify pptx SKILL.md has the slide-type conventions section."""
import os


def test_pptx_skill_has_slide_type_conventions():
    p = os.path.join(
        os.path.dirname(__file__),
        "..", "skills", "pptx", "SKILL.md",
    )
    with open(p) as f:
        content = f.read()
    assert "## Slide-type Conventions" in content
    # Must include the 9 canonical slide types
    for kind in ("Cover", "Section divider", "TOC", "Content", "Data callout",
                 "Comparison", "Quote", "Summary", "Thank you"):
        assert kind in content, f"slide type {kind!r} missing"
    # Must include the rules
    assert "summary slide" in content.lower()
    assert "don't skip" in content.lower() or "Don't skip" in content
