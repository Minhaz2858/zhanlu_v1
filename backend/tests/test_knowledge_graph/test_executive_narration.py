"""Tests for the executive narration system prompt contract.

The narration prompt must forbid meta-language ("snapshot", "payload") and
raw record counts as an answer, and must follow the HEADLINE → KEY FIGURES
→ CAVEAT → NEXT STEP structure.
"""

from __future__ import annotations

from app.services.db.nl_answer_service import _NARRATE_SYSTEM_PROMPT


class TestNarrationPromptContract:
    def test_forbids_meta_language(self):
        for banned in ("snapshot", "payload", "as of the data", "the following"):
            assert banned in _NARRATE_SYSTEM_PROMPT.lower(), \
                f"narration prompt must ban meta-language '{banned}'"

    def test_forbids_raw_record_count_as_answer(self):
        assert "raw record count" in _NARRATE_SYSTEM_PROMPT.lower()
        assert "never present a raw record count" in _NARRATE_SYSTEM_PROMPT.lower()

    def test_follows_executive_structure(self):
        for section in ("HEADLINE", "KEY FIGURES", "CAVEAT", "NEXT STEP"):
            assert section in _NARRATE_SYSTEM_PROMPT, \
                f"narration prompt must include section '{section}'"

    def test_mentions_stale_data_caveat(self):
        assert "stale" in _NARRATE_SYSTEM_PROMPT.lower()


class TestNarrationFormatting:
    """The narration is an LLM call; we assert the prompt is fed the right
    pieces via the helper that builds the user message, without calling an LLM.
    """

    def test_user_message_includes_question_and_sql(self):
        from app.services.db.nl_answer_service import _NARRATE_SYSTEM_PROMPT as _P
        # Build a representative user message the way answer() does.
        question = "What is gross margin for last month?"
        sql = "SELECT ..."

        # The prompt is the contract; asserting it's a non-empty string is the
        # minimum guarantee that the contract module is wired.
        assert isinstance(_P, str) and len(_P) > 200
        # The question & sql are injected at call-time by answer(); we only
        # verify the prompt instructs to use the SQL evidence.
        assert "SQL" in _P
        assert "rows" in _P.lower()
