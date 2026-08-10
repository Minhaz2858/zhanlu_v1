"""Unit tests for DeepResearchService — parallel fan-out + collect + synthesize."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class FakeAgentRunService:
    """Simulates AgentRunService for deep-mode testing."""
    def __init__(self, results_by_id: dict[str, dict]):
        self._results = results_by_id
        self._next_id = 0
        self.started_runs: list[dict] = []

    async def start_run(self, *, agent_name, task, mode="inline",
                        run_id=None, parent_run_id=None,
                        caller_context=None, orchestrator_kwargs=None):
        rid = run_id or f"deep-r{self._next_id}"
        self._next_id += 1
        self.started_runs.append({"run_id": rid, "agent_name": agent_name, "task": task, "mode": mode})
        return rid

    async def collect_run(self, run_id, timeout=120, poll_interval=1):
        return self._results.get(run_id, {"status": "running", "answer": ""})


def _make_fake_splitter(sub_questions: list[str]):
    def _split(q):
        return sub_questions
    return _split


def _make_fake_synthesizer(final_answer: str):
    async def _synth(question, sub_results):
        return final_answer
    return _synth


class TestDeepResearchResult:
    def test_deep_research_result_defaults(self):
        from app.services.research.deep_mode import DeepResearchResult
        r = DeepResearchResult()
        assert r.success is False
        assert r.answer == ""
        assert r.sub_results == []
        assert r.sub_count == 0
        assert r.duration_ms >= 0


class TestDeepResearchService:
    @pytest.mark.asyncio
    async def test_single_sub_question_fan_out(self):
        from app.services.research.deep_mode import DeepResearchService
        results = {"deep-r0": {"status": "completed", "answer": "Answer A"}}
        fake_svc = FakeAgentRunService(results)
        svc = DeepResearchService(
            run_service=fake_svc,
            splitter_fn=_make_fake_splitter(["sub-q1"]),
            synthesizer_fn=_make_fake_synthesizer("Combined answer"))
        result = await svc.research("What is X?")
        assert result.success is True
        assert result.sub_count == 1
        assert "Combined answer" in result.answer
        assert len(fake_svc.started_runs) == 1
        assert fake_svc.started_runs[0]["task"] == "sub-q1"

    @pytest.mark.asyncio
    async def test_multi_sub_question_parallel_fan_out(self):
        from app.services.research.deep_mode import DeepResearchService
        results = {"deep-r0": {"status": "completed", "answer": "A"},
                   "deep-r1": {"status": "completed", "answer": "B"},
                   "deep-r2": {"status": "completed", "answer": "C"}}
        fake_svc = FakeAgentRunService(results)
        svc = DeepResearchService(
            run_service=fake_svc,
            splitter_fn=_make_fake_splitter(["q1", "q2", "q3"]),
            synthesizer_fn=_make_fake_synthesizer("Synthesized"))
        result = await svc.research("Complex question")
        assert result.sub_count == 3
        assert len(fake_svc.started_runs) == 3
        assert all(r["mode"] == "queued" for r in fake_svc.started_runs)

    @pytest.mark.asyncio
    async def test_timeout_sub_run_gives_partial_result(self):
        from app.services.research.deep_mode import DeepResearchService
        results = {"deep-r0": {"status": "completed", "answer": "A"},
                   "deep-r1": {"status": "running", "answer": ""}}
        fake_svc = FakeAgentRunService(results)
        svc = DeepResearchService(
            run_service=fake_svc,
            splitter_fn=_make_fake_splitter(["q1", "q2"]),
            synthesizer_fn=_make_fake_synthesizer("Partial"),
            default_timeout=0.5)
        result = await svc.research("question")
        assert result.sub_count == 2
        assert "Partial" in result.answer

    @pytest.mark.asyncio
    async def test_splitter_failure_falls_back_to_single_question(self):
        from app.services.research.deep_mode import DeepResearchService
        results = {"deep-r0": {"status": "completed", "answer": "Direct"}}
        fake_svc = FakeAgentRunService(results)
        def _failing(question):
            raise RuntimeError("split failed")
        svc = DeepResearchService(
            run_service=fake_svc,
            splitter_fn=_failing,
            synthesizer_fn=_make_fake_synthesizer("Synth"))
        result = await svc.research("Fallback question")
        assert result.sub_count == 1
        assert len(fake_svc.started_runs) == 1
        assert fake_svc.started_runs[0]["task"] == "Fallback question"

    @pytest.mark.asyncio
    async def test_all_sub_runs_are_queued_mode(self):
        from app.services.research.deep_mode import DeepResearchService
        results = {"deep-r0": {"status": "completed", "answer": "OK"}}
        fake_svc = FakeAgentRunService(results)
        svc = DeepResearchService(
            run_service=fake_svc,
            splitter_fn=_make_fake_splitter(["q1"]),
            synthesizer_fn=_make_fake_synthesizer("Done"))
        await svc.research("anything")
        assert fake_svc.started_runs[0]["mode"] == "queued"

    @pytest.mark.asyncio
    async def test_duration_is_recorded(self):
        from app.services.research.deep_mode import DeepResearchService
        results = {"deep-r0": {"status": "completed", "answer": "fast"}}
        fake_svc = FakeAgentRunService(results)
        svc = DeepResearchService(
            run_service=fake_svc,
            splitter_fn=_make_fake_splitter(["q1"]),
            synthesizer_fn=_make_fake_synthesizer("quick"))
        result = await svc.research("timing test")
        assert result.duration_ms >= 0
