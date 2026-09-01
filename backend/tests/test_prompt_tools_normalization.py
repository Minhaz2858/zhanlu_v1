"""Unit tests for `_normalize_prompt_tools_for_bound_kbs`.

Covers: inject when KBs bound, strip when KBs removed, idempotent
(no double-injection), backward compat with migration-script patches
(no marker but already mentions ask_data_agent), and no-op when
no KBs and no marker.
"""

import unittest

from app.services.agent_tools import (
    _normalize_prompt_tools_for_bound_kbs,
    _DB_TOOLS_BLOCK_MARKER_START,
    _DB_TOOLS_BLOCK_MARKER_END,
)


class TestNormalizePromptTools(unittest.TestCase):

    def test_inject_when_kbs_bound(self):
        """When knowledge_bases is non-empty and prompt_tools doesn't
        mention ask_data_agent, the mandatory block is appended."""
        result = _normalize_prompt_tools_for_bound_kbs(
            "Tool selection: use web_search for research.",
            ["kb-1"],
        )
        self.assertIn("ask_data_agent", result)
        self.assertIn(_DB_TOOLS_BLOCK_MARKER_START, result)
        self.assertIn(_DB_TOOLS_BLOCK_MARKER_END, result)
        # Original content preserved
        self.assertIn("web_search", result)

    def test_no_inject_when_no_kbs(self):
        """When knowledge_bases is empty, no block is appended."""
        result = _normalize_prompt_tools_for_bound_kbs(
            "Tool selection: use web_search for research.",
            [],
        )
        self.assertNotIn("ask_data_agent", result)
        self.assertNotIn(_DB_TOOLS_BLOCK_MARKER_START, result)
        self.assertEqual(result, "Tool selection: use web_search for research.")

    def test_no_inject_when_none_kbs(self):
        """When knowledge_bases is None, no block is appended."""
        result = _normalize_prompt_tools_for_bound_kbs(
            "Tool selection: use web_search.",
            None,
        )
        self.assertNotIn("ask_data_agent", result)

    def test_idempotent_inject(self):
        """Running normalize twice with the same KBs doesn't duplicate
        the block."""
        result1 = _normalize_prompt_tools_for_bound_kbs(
            "Use web_search for research.",
            ["kb-1"],
        )
        result2 = _normalize_prompt_tools_for_bound_kbs(result1, ["kb-1"])
        # Count markers — should be exactly 1 start and 1 end
        self.assertEqual(result2.count(_DB_TOOLS_BLOCK_MARKER_START), 1)
        self.assertEqual(result2.count(_DB_TOOLS_BLOCK_MARKER_END), 1)
        self.assertEqual(result1, result2)

    def test_strip_when_kbs_removed(self):
        """When knowledge_bases changes from non-empty to empty, the
        previously injected block is stripped."""
        injected = _normalize_prompt_tools_for_bound_kbs(
            "Use web_search for research.",
            ["kb-1"],
        )
        # Now remove KBs
        stripped = _normalize_prompt_tools_for_bound_kbs(injected, [])
        self.assertNotIn(_DB_TOOLS_BLOCK_MARKER_START, stripped)
        self.assertNotIn(_DB_TOOLS_BLOCK_MARKER_END, stripped)
        # Original content preserved
        self.assertIn("web_search", stripped)
        self.assertNotIn("ask_data_agent", stripped)

    def test_backward_compat_migration_script_patch(self):
        """The migration script wrote prompt_tools that mentions
        ask_data_agent but lacks the marker. Running normalize should
        NOT add another block (idempotent via ask_data_agent check)."""
        migration_text = (
            "Tool selection: when the user asks about data, you MUST "
            "call the tool whose function name is exactly `ask_data_agent`."
        )
        result = _normalize_prompt_tools_for_bound_kbs(migration_text, ["kb-1"])
        # Should NOT add the marker block — ask_data_agent already present
        self.assertNotIn(_DB_TOOLS_BLOCK_MARKER_START, result)
        # ask_data_agent still present (from the original text)
        self.assertIn("ask_data_agent", result)
        # Content unchanged
        self.assertEqual(result, migration_text)

    def test_backward_compat_migration_then_remove_kbs(self):
        """When a migration-patched prompt_tools (no marker) has its
        KBs removed, the text stays as-is (can't strip without marker)."""
        migration_text = (
            "Tool selection: call `ask_data_agent` for database queries."
        )
        result = _normalize_prompt_tools_for_bound_kbs(migration_text, [])
        # No marker to strip, text unchanged
        self.assertEqual(result, migration_text)
        # ask_data_agent reference remains (won't hurt — runtime won't
        # inject the tool if knowledge_bases is empty)

    def test_none_prompt_tools_with_kbs(self):
        """When prompt_tools is None and KBs are bound, returns None
        (the helper doesn't fabricate content from scratch)."""
        result = _normalize_prompt_tools_for_bound_kbs(None, ["kb-1"])
        self.assertIsNone(result)

    def test_empty_prompt_tools_with_kbs(self):
        """When prompt_tools is empty string and KBs are bound, the
        block is injected."""
        result = _normalize_prompt_tools_for_bound_kbs("", ["kb-1"])
        self.assertIn("ask_data_agent", result)
        self.assertIn(_DB_TOOLS_BLOCK_MARKER_START, result)

    def test_strip_then_inject_cycle(self):
        """Simulating a KB bind → unbind → rebind cycle works correctly."""
        text = "Use web_search for research."
        # Bind KB
        step1 = _normalize_prompt_tools_for_bound_kbs(text, ["kb-1"])
        self.assertIn("ask_data_agent", step1)
        # Unbind KB
        step2 = _normalize_prompt_tools_for_bound_kbs(step1, [])
        self.assertNotIn("ask_data_agent", step2)
        # Rebind KB
        step3 = _normalize_prompt_tools_for_bound_kbs(step2, ["kb-1"])
        self.assertIn("ask_data_agent", step3)
        # Only one block
        self.assertEqual(step3.count(_DB_TOOLS_BLOCK_MARKER_START), 1)


if __name__ == "__main__":
    unittest.main()
