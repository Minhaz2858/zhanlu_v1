"""Tests for the empty-answer safety net (Fix 2) in agents.py.

A reasoning model (e.g. deepseek-v4-flash) can burn its output budget on
``reasoning_content`` and end the turn with ``content=""`` even though it
retrieved usable data. ``_empty_answer_needs_force`` decides whether the
v3 loop must force ONE re-synthesis pass instead of letting the generic
empty-content fallback fire.
"""

from __future__ import annotations

from app.routers.agents import _empty_answer_needs_force


class TestEmptyAnswerNeedsForce:
    def test_forces_when_empty_with_usable_data(self):
        assert _empty_answer_needs_force("", False, [], 0, True) is True

    def test_no_force_when_content_exists(self):
        assert _empty_answer_needs_force("answer", False, [], 0, True) is False

    def test_forces_even_when_content_streamed(self):
        # content_streamed=True means deltas were delivered, but those
        # deltas were intermediate narration (promises), not a real answer.
        # The net now fires regardless — streamed promise text is not a
        # substitute for a synthesized final answer.
        assert _empty_answer_needs_force("", True, [], 0, True) is True

    def test_forces_even_when_earlier_prose_exists(self):
        # Earlier iterations may have produced prose (promise narration,
        # partial findings). The final reply is what matters — if it's
        # empty, force synthesis.
        assert _empty_answer_needs_force("", False, ["earlier"], 0, True) is True

    def test_no_force_when_budget_spent(self):
        # Cap is 1 force per turn; a second empty answer falls through.
        assert _empty_answer_needs_force("", False, [], 1, True) is False

    def test_no_force_without_usable_data(self):
        assert _empty_answer_needs_force("", False, [], 0, False) is False
