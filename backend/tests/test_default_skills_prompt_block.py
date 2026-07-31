"""Verify the default-skills prompt block mentions run_sandbox_skill + markers."""
import importlib


def test_default_skills_block_mentions_run_sandbox_skill():
    """The block must tell the LLM to use run_sandbox_skill for tool-heavy
    file generation, otherwise agents try to call pandoc/LibreOffice
    inline and fail."""
    from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK
    assert "run_sandbox_skill" in _DEFAULT_SKILLS_BLOCK, (
        "Default skills block must mention run_sandbox_skill so agents "
        "know to use the sandbox for tool-heavy file generation"
    )


def test_default_skills_block_mentions_marker_contract():
    """The block must tell the LLM to emit ◤MD_DOCX◤ / ◤PPTX◤ markers
    at the end of the reply, otherwise the marker runtime never fires."""
    from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK
    assert "◤MD_DOCX◤" in _DEFAULT_SKILLS_BLOCK
    assert "◤PPTX◤" in _DEFAULT_SKILLS_BLOCK


def test_default_skills_block_is_non_empty():
    """The block must be present and contain the skill entries."""
    from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK
    assert len(_DEFAULT_SKILLS_BLOCK) > 200, (
        f"Block suspiciously short ({len(_DEFAULT_SKILLS_BLOCK)} chars) — "
        "is default_skills importable from agent_prompts?"
    )
    assert "DEFAULT SKILLS" in _DEFAULT_SKILLS_BLOCK


def test_default_skills_block_mentions_skill_view():
    """skill_view is the progressive-disclosure entry point."""
    from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK
    assert "skill_view" in _DEFAULT_SKILLS_BLOCK
