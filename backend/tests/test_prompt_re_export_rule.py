"""Smoke test for the RE-EXPORT / RE-FORMAT HARD RULE prompt fragment.

Task 14: the default skills block must teach the LLM to reuse the cached
SESSION STATE execution_id (create_artifact(source_execution_id=...))
instead of re-running data tools when the user asks to re-export or
re-format a previous analysis.
"""


def test_default_skills_block_contains_re_export_rule():
    from app.services.agent_prompts import _build_default_skills_block
    block = _build_default_skills_block()
    assert "RE-EXPORT" in block or "RE-FORMAT" in block
    assert "source_execution_id" in block
