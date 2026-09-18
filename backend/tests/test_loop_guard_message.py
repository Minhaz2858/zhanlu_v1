"""Loop-guard copy + the "don't discard the work" contract.

Regression cover for a live failure (2026-09-18): the tool-call loop guard
fired on a legitimate multi-part data question (four ask_data_agent calls with
DIFFERENT questions, each returning correct rows) and responded to the user with

    "I'm going to build the agent with sensible defaults now."

— the agent-BUILDER string, hardcoded at every guard site — while `break`ing
before a final LLM call, so all four correct tool results were thrown away.
The user asked for sales from the database and was told an agent was being built.

The guard now (a) speaks in the right domain and (b) grants ONE tool-free pass so
the gathered results become the answer.
"""

from __future__ import annotations

import inspect
import re

import pytest


# ── agent-appropriate copy ────────────────────────────────────────────

class TestLoopGuardMessage:
    def test_agent_builder_keeps_build_copy(self):
        from app.routers.agents import _loop_guard_message
        msg = _loop_guard_message("agent_builder")
        assert "build the agent" in msg

    @pytest.mark.parametrize("agent", [
        "general_assistant", "Sales Agent", "Research Assistant",
        "Data Analyst", "bi_assistant", "", None,
    ])
    def test_non_builder_agents_never_mention_building_an_agent(self, agent):
        """A data/sales question must never be answered with agent-builder copy."""
        from app.routers.agents import _loop_guard_message
        msg = _loop_guard_message(agent)
        assert "build the agent" not in msg
        assert "after creation" not in msg
        assert msg.strip()

    def test_generic_copy_is_domain_neutral(self):
        """No domain nouns — it is used by every non-builder agent."""
        from app.routers.agents import _loop_guard_message
        msg = _loop_guard_message("general_assistant").lower()
        for leaked in ("database", "sql", "sales", "table", "metric", "chart"):
            assert leaked not in msg


class TestLoopGuardNudge:
    def test_nudge_tells_model_to_answer_without_tools(self):
        from app.routers.agents import _loop_guard_answer_nudge
        nudge = _loop_guard_answer_nudge("ask_data_agent", 10)
        assert "ask_data_agent" in nudge          # LLM-facing: tool name is fine
        assert "Do not call any tools" in nudge
        assert "final answer" in nudge


# ── structural guards: the defect cannot come back ────────────────────

class TestLoopGuardCallSites:
    """Source-level contracts on app/routers/agents.py."""

    @pytest.fixture(scope="class")
    def src(self):
        from app.routers import agents
        return inspect.getsource(agents)

    def test_builder_copy_appears_once_and_only_as_the_constant(self, src):
        """Every guard site must call _loop_guard_message().

        If the literal reappears inline, some agent is again being told an
        agent is being built.
        """
        hits = [
            ln for ln in src.splitlines()
            if "build the agent with sensible defaults" in ln
            and not ln.strip().startswith("#")   # the explanatory comment is fine
        ]
        assert len(hits) == 1, (
            "loop-guard builder copy must exist exactly once (the constant "
            f"_LOOP_GUARD_AGENT_BUILDER_MSG); found {len(hits)}: {hits}"
        )

    def test_every_guard_site_uses_the_helper(self, src):
        """One helper call per loop (main, resume, v3 stream).

        Each of the three loops has its own first-trip and second-trip logging
        branch, but exactly one user-facing message call.
        """
        helper_calls = src.count("_loop_guard_message(agent_name)")
        assert helper_calls == 3, (
            f"expected 3 loop-guard sites to use _loop_guard_message(agent_name); "
            f"found {helper_calls} — a site may still hardcode its user-facing text"
        )

    def test_guard_grants_a_synthesis_pass(self, src):
        """The one-tool-free-pass flag must be initialised and consumed."""
        assert src.count("_loop_guard_synthesis_used = False") >= 3, (
            "each loop (main, resume, v3 stream) must initialise the flag"
        )
        assert src.count("_loop_guard_synthesis_used = True") >= 3, (
            "each guard must grant the pass on first trip"
        )
        assert src.count("if _loop_guard_synthesis_used:") >= 3, (
            "each guard must stop only on the SECOND trip"
        )
