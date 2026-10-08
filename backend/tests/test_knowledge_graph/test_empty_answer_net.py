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

    def test_no_force_when_content_is_a_real_answer(self):
        # Contract tightened 2026-09-18: "non-empty" is no longer sufficient.
        # A reply that carries a figure IS an answer — do not re-synthesize.
        assert _empty_answer_needs_force(
            "本周示例工厂销售额 17,772,540 元，共 12 张单据。", False, [], 0, True
        ) is False

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


class TestNonAnswerNarration:
    """Data was retrieved, but the reply is process/compliance talk.

    Live case 2026-09-18: five completed data tool calls returned the correct
    totals (17,772,540.00 / 15,727,911.50) and the user's whole answer was

        "没有创建文档，原因是 你明确要求了'不要输出文档'…"

    i.e. narration ABOUT the request instead of the answer. The reply must be
    re-synthesized once. The test is domain/database-neutral by design: data
    came back, and the reply contains no figure at all.
    """

    LIVE_COMPLIANCE_NARRATION = (
        "没有创建文档，原因是**你明确要求了\"不要输出文档\"**——没有任何技术障碍，"
        "数据齐全、格式也支持，纯粹是遵照你的指令。我不会覆盖你的显式要求去生成附件。"
    )

    def test_forces_on_live_compliance_narration(self):
        assert _empty_answer_needs_force(
            self.LIVE_COMPLIANCE_NARRATION, False, [], 0, True
        ) is True

    def test_forces_on_english_process_narration(self):
        assert _empty_answer_needs_force(
            "I did not create a document because you asked me not to. "
            "The data is ready and I can export it on request.",
            False, [], 0, True,
        ) is True

    def test_forces_on_promise_narration_without_figures(self):
        assert _empty_answer_needs_force(
            "Let me pull this week's numbers for you.", False, [], 0, True
        ) is True

    def test_no_force_when_no_data_was_retrieved(self):
        # Digit-free prose is legitimate when nothing was queried (chat, advice).
        assert _empty_answer_needs_force(
            "Happy to help — tell me which region you care about.", False, [], 0, False
        ) is False

    def test_no_force_for_long_digit_free_answer(self):
        # A long qualitative answer is a real answer, not narration.
        assert _empty_answer_needs_force("x" * 700, False, [], 0, True) is False

    def test_no_force_when_force_already_used(self):
        # Still capped at ONE forced pass per turn — no infinite retry.
        assert _empty_answer_needs_force(
            self.LIVE_COMPLIANCE_NARRATION, False, [], 1, True
        ) is False
