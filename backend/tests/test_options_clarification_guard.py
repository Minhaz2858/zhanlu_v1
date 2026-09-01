"""Regression tests for the :::options clarification hard-stop guard.

The Skill Agent (and any agent) emits a :::options block to ask the user a
clarifying question. Weak LLMs ignore the prose rule in SKILL_AGENT_SYSTEM_PROMPT
and run research tools (web_search / search_skills) in the SAME turn while
waiting for the user to click chips. The runtime guard in the three agent loops
(``_options_clarification``) suppresses those tool calls deterministically.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers.agents import _options_clarification  # noqa: E402

OPTIONS_BLOCK = (
    "I'll commit to defaults: analysts, markdown, on-demand.\n"
    ":::options\n"
    "- Audience: Analysts & business teams\n"
    "- Audience: Executives (CEO / leadership)\n"
    "- Output: Markdown (exportable to DOCX/PDF)\n"
    ":::\n"
)


class TestOptionsClarificationGuard:
    def test_options_block_with_web_search_suppressed(self):
        # The observed bug: options block + web_search in the same turn.
        assert _options_clarification(OPTIONS_BLOCK, ["web_search"]) is True

    def test_options_block_with_search_skills_suppressed(self):
        assert _options_clarification(OPTIONS_BLOCK, ["search_skills"]) is True

    def test_options_block_no_tools_ends_turn(self):
        # Even without tool calls, a clarification block ends the turn
        # (skip verification / goal-contract gates).
        assert _options_clarification(OPTIONS_BLOCK, []) is True

    def test_unclosed_options_block_still_suppressed(self):
        # The frontend renders unclosed blocks too — the guard must not
        # depend on a clean ":::" closing line at the very end.
        unclosed = "Defaults: analysts, markdown.\n:::options\n- Output: Markdown\n- Output: PDF"
        assert _options_clarification(unclosed, ["web_search"]) is True

    def test_create_skill_with_mention_not_suppressed(self):
        # skill_md may legitimately mention :::options in its methodology —
        # skill-writing calls must NOT be suppressed.
        content = "Creating the skill now. The skill instructs agents to use :::options blocks when clarifying."
        assert _options_clarification(content, ["create_skill"]) is False

    def test_normal_content_no_options(self):
        assert _options_clarification("Here is the completed report.", ["web_search"]) is False

    def test_mixed_create_and_research_suppressed(self):
        # If the model pairs create_skill WITH research tools after an options
        # block, the research is still suppressed (all-or-nothing rule).
        assert _options_clarification(OPTIONS_BLOCK, ["create_skill", "web_search"]) is True

    def test_empty_content(self):
        assert _options_clarification("", ["web_search"]) is False
