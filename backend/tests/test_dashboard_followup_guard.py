"""Regression tests — unwanted artifact creation (2026-10-19).

Live failure (conv f9712bfd): "give me last week sales overview" — a plain
data question on a FRESH conversation — was forced through the dashboard
follow-up path. The TURN PLAN block is injected into ``llm_messages`` as
``role="user"`` (vLLM rejects mid-list system messages); the follow-up
heuristic ``_prior_user_turn_was_live_dashboard`` read that scaffold as the
user's PREVIOUS chat message. The dynamic plan's build step said
"Build the live dashboard", so ``is_live_dashboard_request(plan_block)``
returned True for EVERY data question. Consequences observed live:

1. ``tool_choice=create_fullstack_dashboard`` forced at iteration 0.
2. DeepSeek 400'd the forced tool_choice → retry without tool_choice.
3. The retried model call, still steered by the build-oriented plan,
   called ``create_artifact(type="html")`` → a 72 KB HTML file instead of
   the inline answer the user asked for. The "inline chat" follow-up got
   identical treatment ("Building your deliverable" again).

Fix under test: the scan must SKIP injected scaffold blocks (TURN PLAN)
and only consider REAL user turns.
"""

import pytest


def _plan_block(step_title: str = "Build the live dashboard") -> str:
    """Shape produced by turn_planner.plan_to_system_block (role=user)."""
    return (
        "TURN PLAN — the user's request has been decomposed into the following "
        "steps. Execute them IN ORDER. Do not skip ahead, do not repeat a step "
        "that is already done, and do not narrate the plan — execute it.\n"
        "1. Understand the question\n"
        "2. Query the bound data source\n"
        "3. Analyze the results\n"
        f"4. {step_title}\n"
    )


class TestDashboardFollowupIgnoresScaffolds:
    def _fn(self):
        from app.routers.agents import _prior_user_turn_was_live_dashboard
        return _prior_user_turn_was_live_dashboard

    def test_plan_block_mentioning_dashboard_is_not_a_followup(self):
        """THE live failure: only history = system + plan block + current msg.

        The plan block's build step says "Build the live dashboard" — the
        scan must NOT treat that as a prior dashboard request.
        """
        fn = self._fn()
        llm_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "give me last week sales overview"},
            {"role": "user", "content": _plan_block()},
        ]
        assert fn(llm_messages, "give me last week sales overview") is False

    def test_plan_block_with_dashboard_title_not_followup(self):
        fn = self._fn()
        llm_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": _plan_block(
                "Create a sales dashboard with KPIs")},
        ]
        assert fn(llm_messages, "give me last week sales overview") is False

    def test_real_prior_dashboard_request_still_matches(self):
        """The heuristic's purpose is preserved: genuine prior dashboard
        turns (conv 8b4d1968 regression) still count as follow-ups."""
        fn = self._fn()
        llm_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "build a sales dashboard"},
            {"role": "assistant", "content": "building..."},
            {"role": "user", "content": "give me last week sales overview"},
            {"role": "user", "content": _plan_block()},
        ]
        assert fn(llm_messages, "give me last week sales overview") is True

    def test_empty_history_returns_false(self):
        fn = self._fn()
        assert fn([], "anything") is False
        assert fn(None, "anything") is False

    def test_current_message_dashboard_word_not_self_match(self):
        """The current message itself must be skipped, not matched."""
        fn = self._fn()
        llm_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "open the dashboard"},
        ]
        assert fn(llm_messages, "open the dashboard") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
