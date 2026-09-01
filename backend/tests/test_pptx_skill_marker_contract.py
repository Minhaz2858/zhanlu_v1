"""Verify pptx SKILL.md has the ◤PPTX◤ marker contract section."""
import os


def test_pptx_skill_documents_pptx_marker():
    p = os.path.join(
        os.path.dirname(__file__),
        "..", "skills", "pptx", "SKILL.md",
    )
    with open(p) as f:
        content = f.read()
    # The marker itself (with the ◤ characters)
    assert "◤PPTX◤" in content
    assert "◤END_PPTX◤" in content
    # The example payload shape
    assert '"slides_path"' in content
    assert '"filename"' in content
    # The end-of-reply rule
    assert "END of your reply" in content or "end of your reply" in content
