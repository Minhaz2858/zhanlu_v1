"""Tests for the shared conversation tone block and rewritten system prompts.

Verifies:
1. _CONVERSATION_TONE_BLOCK is appended to ALL agent prompt paths
2. Tone block content includes warmth, conciseness, and no-preamble guidance
3. Tool-listing bloat removed from general_assistant prompt
4. Anti-hallucination over-correction fixed (general knowledge OK)
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


class TestConversationToneBlock(unittest.TestCase):
    def test_tone_block_constant_exists_and_has_content(self):
        from app.services.agent_prompts import _CONVERSATION_TONE_BLOCK

        self.assertIsInstance(_CONVERSATION_TONE_BLOCK, str)
        self.assertGreater(len(_CONVERSATION_TONE_BLOCK), 100)

    def test_tone_block_includes_warmth_and_conciseness(self):
        from app.services.agent_prompts import _CONVERSATION_TONE_BLOCK

        lower = _CONVERSATION_TONE_BLOCK.lower()
        self.assertIn("warm", lower)
        self.assertIn("concise", lower)
        # No corporate preamble filler.
        self.assertIn("preamble", lower)


class TestToneBlockAppendedToAllPaths(unittest.TestCase):
    """get_system_prompt must append the tone block on every path."""

    def test_general_assistant_prompt_has_tone_block(self):
        from app.services.agent_prompts import (
            get_system_prompt, _CONVERSATION_TONE_BLOCK,
        )

        prompt = get_system_prompt("general_assistant")
        self.assertIn(_CONVERSATION_TONE_BLOCK, prompt)

    def test_power_user_prompt_has_tone_block(self):
        from app.services.agent_prompts import (
            get_system_prompt, _CONVERSATION_TONE_BLOCK,
        )

        prompt = get_system_prompt("power_user")
        self.assertIn(_CONVERSATION_TONE_BLOCK, prompt)

    def test_generic_fallback_prompt_has_tone_block(self):
        from app.services.agent_prompts import (
            get_system_prompt, _CONVERSATION_TONE_BLOCK,
        )

        prompt = get_system_prompt("nonexistent_agent_xyz")
        self.assertIn(_CONVERSATION_TONE_BLOCK, prompt)

    def test_user_created_agent_prompt_has_tone_block(self):
        """User-created agents go through assemble_user_agent_prompt —
        the tone block must be appended there too."""
        from app.services.agent_prompts import (
            get_system_prompt, _CONVERSATION_TONE_BLOCK,
        )

        # Simulate a user-created agent app with minimal fields.
        agent_app = MagicMock()
        agent_app.name = "My Custom Agent"
        agent_app.description = "A test agent"
        agent_app.agent_definition = "You are a helpful test agent."
        agent_app.capabilities = []
        agent_app.constraints = []
        agent_app.skills = []
        agent_app.tools = ["web_search"]
        agent_app.system_prompt_override = None

        prompt = get_system_prompt("custom_agent_123", agent_app=agent_app)
        self.assertIn(_CONVERSATION_TONE_BLOCK, prompt)


class TestGeneralAssistantPromptRewritten(unittest.TestCase):
    def test_tool_listing_bloat_removed(self):
        """The 25-line tool-by-tool listing should be gone — tools are
        function definitions, not prompt text."""
        from app.services.agent_prompts import GENERAL_ASSISTANT_SYSTEM_PROMPT

        # These were the bloat lines — none should appear as standalone
        # tool-description entries anymore.
        self.assertNotIn("fuzzy_match: Robust find-and-replace", GENERAL_ASSISTANT_SYSTEM_PROMPT)
        self.assertNotIn("osv_check: CVE lookup", GENERAL_ASSISTANT_SYSTEM_PROMPT)
        self.assertNotIn("tirith_security: Pre-flight shell command safety scan", GENERAL_ASSISTANT_SYSTEM_PROMPT)
        self.assertNotIn("env_passthrough / credential_files", GENERAL_ASSISTANT_SYSTEM_PROMPT)

    def test_anti_hallucination_fixed(self):
        """The old prompt said 'For ANY factual question you MUST call a
        tool FIRST' — that's gone. The new version allows answering
        general knowledge directly."""
        from app.services.agent_prompts import GENERAL_ASSISTANT_SYSTEM_PROMPT

        lower = GENERAL_ASSISTANT_SYSTEM_PROMPT.lower()
        # Old over-correction removed.
        self.assertNotIn("for any factual, current, externally-checkable", lower)
        self.assertNotIn("never answer from training-data memory", lower)
        # New guidance present.
        self.assertIn("general knowledge", lower)
        self.assertIn("answer directly", lower)

    def test_file_format_intent_is_advisory_not_rigid(self):
        """The old prompt had 'HARD RULE' + numbered pipeline. The new
        version is advisory guidance."""
        from app.services.agent_prompts import GENERAL_ASSISTANT_SYSTEM_PROMPT

        lower = GENERAL_ASSISTANT_SYSTEM_PROMPT.lower()
        self.assertNotIn("file-format intent (hard rule", lower)
        self.assertNotIn("in this exact order", lower)
        # Advisory guidance still present.
        self.assertIn("ask_data_agent", lower)
        self.assertIn("run_sandbox_skill", lower)


if __name__ == "__main__":
    unittest.main()
