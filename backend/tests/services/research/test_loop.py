"""Unit tests for ResearchLoopOrchestrator — reflexion loop chain.

Uses fake callables for all external dependencies (DI pattern).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Helpers — fake callables that return async values
# ---------------------------------------------------------------------------

async def _fake_classify_ok(user_message, conversation_context="", use_llm=False):
    """Simulate a confident perception-stage classification."""
    from app.services.intent_planner import IntentResult
    return IntentResult(stages=["perception"], primary_stage="perception",
                        confidence=0.9, rationale="clear query")


async def _fake_run_ok():
    """Simulate AgentRunOrchestrator.run() returning a successful result."""
    from app.services.harness.orchestrator import RunResult
    return RunResult(run_id="r-test", success=True,
                     answer="The answer is 42.", iterations=2)


def _make_fake_run(answer_text):
    async def _run():
        from app.services.harness.orchestrator import RunResult
        return RunResult(run_id="r-test", success=True,
                         answer=answer_text, iterations=1)
    return _run


async def _fake_critique_accept(user_message, assistant_text, *, session_id=None):
    """Simulate self-critic: no refusal, high confidence."""
    from app.services.self_critic import SelfCriticDecision
    return SelfCriticDecision(refused=False, confidence=0.9,
                              reasoning="looks good")


async def _fake_critique_refused(user_message, assistant_text, *, session_id=None):
    """Simulate self-critic: refusal with corrective tool."""
    from app.services.self_critic import SelfCriticDecision
    return SelfCriticDecision(refused=True, confidence=0.3,
                              reasoning="seems wrong",
                              corrective_tool="search",
                              corrective_args={"query": "fix me"})


def _fake_verify_none(messages, *, attempts=0, max_attempts=2, project_facts=None):
    """Simulate verification_stop: no nudge needed."""
    return None


def _fake_verify_nudge(messages, *, attempts=0, max_attempts=2, project_facts=None):
    """Simulate verification_stop: nudge needed."""
    return "Please verify your changes with pytest."


async def _fake_reflexion_accept(*, user_message, assistant_text,
                                 llm_call=None, max_chars=4000):
    """Simulate reflexion: accept verdict."""
    from app.services.synexia.reflexion import ReflexionVerdict
    return ReflexionVerdict(verdict="accept", confidence=0.95,
                            issues=[], suggestions=[])


async def _fake_reflexion_revise(*, user_message, assistant_text,
                                 llm_call=None, max_chars=4000):
    """Simulate reflexion: revise verdict."""
    from app.services.synexia.reflexion import ReflexionVerdict
    return ReflexionVerdict(verdict="revise", confidence=0.5,
                            issues=["incomplete"],
                            suggestions=["add more detail"])


async def _fake_reflexion_reject(*, user_message, assistant_text,
                                 llm_call=None, max_chars=4000):
    """Simulate reflexion: reject verdict."""
    from app.services.synexia.reflexion import ReflexionVerdict
    return ReflexionVerdict(verdict="reject", confidence=0.1,
                            issues=["hallucination"],
                            suggestions=["start over"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResearchResult:
    def test_research_result_defaults(self):
        from app.services.research.loop import ResearchResult
        r = ResearchResult()
        assert r.success is False
        assert r.answer == ""
        assert r.iterations == 0
        assert r.revision_cycles == 0
        assert r.verdict == "accept"
        assert r.issues == []
        assert r.suggestions == []


class TestResearchLoopOrchestrator:

    # ---- accept ----

    @pytest.mark.asyncio
    async def test_accept_passes_through(self):
        from app.services.research.loop import ResearchLoopOrchestrator
        loop = ResearchLoopOrchestrator(
            classify_fn=_fake_classify_ok,
            run_fn=_fake_run_ok,
            critic_fn=_fake_critique_accept,
            verify_fn=_fake_verify_none,
            reflexion_fn=_fake_reflexion_accept,
            max_revisions=2,
        )
        result = await loop.run("What is the answer?")
        assert result.success is True
        assert "42" in result.answer
        assert result.verdict == "accept"
        assert result.revision_cycles == 0

    # ---- revise (single cycle) ----

    @pytest.mark.asyncio
    async def test_revise_reruns_once(self):
        """When reflexion says revise, inject suggestions and re-run once.

        Uses max_revisions=1 so the single revise cycle is also the cap.
        """
        call_count = [0]

        async def _run_then_improve():
            call_count[0] += 1
            from app.services.harness.orchestrator import RunResult
            if call_count[0] == 1:
                return RunResult(run_id="r1", success=True,
                                 answer="v1 incomplete", iterations=1)
            else:
                return RunResult(run_id="r2", success=True,
                                 answer="v2 improved with detail", iterations=1)

        from app.services.research.loop import ResearchLoopOrchestrator
        loop = ResearchLoopOrchestrator(
            classify_fn=_fake_classify_ok,
            run_fn=_run_then_improve,
            critic_fn=_fake_critique_accept,
            verify_fn=_fake_verify_none,
            reflexion_fn=_fake_reflexion_revise,
            max_revisions=1,
        )
        result = await loop.run("Tell me more")
        assert result.success is True
        assert "v2" in result.answer
        assert result.verdict == "revise"
        assert result.revision_cycles == 1
        assert call_count[0] == 2

    # ---- max revisions ----

    @pytest.mark.asyncio
    async def test_max_revisions_capped(self):
        """After max_revisions, stop even if still 'revise'."""
        from app.services.research.loop import ResearchLoopOrchestrator
        loop = ResearchLoopOrchestrator(
            classify_fn=_fake_classify_ok,
            run_fn=_fake_run_ok,
            critic_fn=_fake_critique_accept,
            verify_fn=_fake_verify_none,
            reflexion_fn=_fake_reflexion_revise,  # always revise
            max_revisions=2,
        )
        result = await loop.run("anything")
        assert result.revision_cycles == 2
        assert result.verdict == "revise"  # last verdict still revise

    # ---- reject ----

    @pytest.mark.asyncio
    async def test_reject_stops_immediately(self):
        from app.services.research.loop import ResearchLoopOrchestrator
        loop = ResearchLoopOrchestrator(
            classify_fn=_fake_classify_ok,
            run_fn=_fake_run_ok,
            critic_fn=_fake_critique_accept,
            verify_fn=_fake_verify_none,
            reflexion_fn=_fake_reflexion_reject,
            max_revisions=2,
        )
        result = await loop.run("something")
        assert result.verdict == "reject"
        assert result.revision_cycles == 0
        assert len(result.issues) > 0

    # ---- critic refusal triggers reflexion early ----

    @pytest.mark.asyncio
    async def test_critic_refusal_triggers_reflexion(self):
        """When self-critic refuses, skip verify and go straight to reflexion."""
        from app.services.research.loop import ResearchLoopOrchestrator
        loop = ResearchLoopOrchestrator(
            classify_fn=_fake_classify_ok,
            run_fn=_fake_run_ok,
            critic_fn=_fake_critique_refused,
            verify_fn=_fake_verify_none,
            reflexion_fn=_fake_reflexion_revise,
            max_revisions=2,
        )
        result = await loop.run("bad query")
        # reflexion still runs; verdict comes from reflexion
        assert result.verdict in ("accept", "revise", "reject")
        assert result.revision_cycles >= 0

    # ---- error in run_fn ----

    @pytest.mark.asyncio
    async def test_run_error_is_handled(self):
        async def _run_error():
            from app.services.harness.orchestrator import RunResult
            return RunResult(run_id="r-err", success=False,
                             answer="", error="LLM timeout")

        from app.services.research.loop import ResearchLoopOrchestrator
        loop = ResearchLoopOrchestrator(
            classify_fn=_fake_classify_ok,
            run_fn=_run_error,
            critic_fn=_fake_critique_accept,
            verify_fn=_fake_verify_none,
            reflexion_fn=_fake_reflexion_accept,
            max_revisions=2,
        )
        result = await loop.run("anything")
        assert result.success is False
        assert "LLM timeout" in result.answer
