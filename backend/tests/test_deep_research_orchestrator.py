"""Tests for the deep-research orchestrator (v1 in-loop research protocol).

Covers:
  - Depth presets (quick/balanced/deep → gather + critique rounds)
  - Request validation (depth/scope/question)
  - Scope → tool mapping (web / internal / both)
  - Plan fallback when the planner returns malformed JSON
  - Phase event sequence (plan → gather [→ critique] → finalize)
  - Critic round counts per depth
  - Citation parsing on the final report

Red-first: fails until ``app/services/deep_research/orchestrator.py`` exists.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.deep_research.orchestrator import (
    DEPTH_PRESETS,
    DeepResearchRequest,
    DeepResearchResult,
    SCOPE_TOOLS,
    run_deep_research,
)


# ── Fakes ─────────────────────────────────────────────────────────────

class FakePlanner:
    def __init__(self, plan: dict):
        self.plan = plan
        self.calls = 0

    async def __call__(self, messages):
        self.calls += 1
        return self.plan


class FakeToolExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name, args):
        self.calls.append((name, args))
        return {"rows": [{"k": "v"}], "url": "https://example.com"}


class FakeSynthesizer:
    def __init__(self, report: str):
        self.report = report
        self.calls = 0
        self.last_messages = None

    async def __call__(self, messages):
        self.calls += 1
        self.last_messages = messages
        return self.report


class FakeCritic:
    def __init__(self, gap: str = "Need more recent data."):
        self.gap = gap
        self.calls = 0

    async def __call__(self, messages):
        self.calls += 1
        return self.gap


class EventCollector:
    def __init__(self):
        self.events: list[tuple[str, str | None]] = []

    async def __call__(self, event_type, label_key=None, params=None):
        self.events.append((event_type, label_key))


def _plan(steps):
    return {"steps": steps}


def _step(q="What is X?", tools=("web_search",), sid="s1"):
    return {"id": sid, "question": q, "tools": list(tools), "rationale": "test"}


# ── Depth presets ─────────────────────────────────────────────────────

def test_depth_presets_quick():
    assert DEPTH_PRESETS["quick"] == {"gather_rounds": 1, "critique_rounds": 0, "max_plan_steps": 4}


def test_depth_presets_balanced():
    assert DEPTH_PRESETS["balanced"] == {"gather_rounds": 2, "critique_rounds": 1, "max_plan_steps": 6}


def test_depth_presets_deep():
    assert DEPTH_PRESETS["deep"] == {"gather_rounds": 3, "critique_rounds": 2, "max_plan_steps": 8}


# ── Request validation ────────────────────────────────────────────────

def test_request_valid_depth_and_scope():
    req = DeepResearchRequest(question="Q", depth="deep", scope="internal")
    assert req.depth == "deep"
    assert req.scope == "internal"


def test_request_rejects_unknown_depth():
    with pytest.raises(ValueError):
        DeepResearchRequest(question="Q", depth="ultra")


def test_request_rejects_unknown_scope():
    with pytest.raises(ValueError):
        DeepResearchRequest(question="Q", scope="cloud")


def test_request_rejects_empty_question():
    with pytest.raises(ValueError):
        DeepResearchRequest(question="  ")


# ── Scope → tool mapping ──────────────────────────────────────────────

def test_scope_tool_map():
    assert set(SCOPE_TOOLS["web"]) == {"web_search", "web_extract"}
    assert set(SCOPE_TOOLS["internal"]) == {"sql_query", "ask_data_agent", "comprehensive_data"}


# ── Orchestrator: quick depth ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_quick_runs_one_gather_no_critique():
    tools = FakeToolExecutor()
    planner = FakePlanner(_plan([_step(), _step(sid="s2", tools=["web_extract"])]))
    synthesizer = FakeSynthesizer("Report [source: s1].")
    critic = FakeCritic()
    events = EventCollector()

    result = await run_deep_research(
        DeepResearchRequest(question="Q", depth="quick", scope="web"),
        planner=planner,
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=critic,
        emit=events,
    )

    assert isinstance(result, DeepResearchResult)
    assert result.report == "Report [source: s1]."
    assert planner.calls == 1
    assert synthesizer.calls == 1
    assert critic.calls == 0  # quick depth has no critique round
    assert len(tools.calls) == 2  # one gather round, two plan steps
    labels = [label or "" for _, label in events.events]
    assert "phase_enter.research_plan" in labels
    assert "phase_enter.research_gather" in labels
    assert "phase_enter.research_critique" not in labels
    assert "phase_enter.research_finalize" in labels
    types = [t for t, _ in events.events]
    assert types.count("plan_step_added") == 2


# ── Orchestrator: balanced depth ──────────────────────────────────────

@pytest.mark.asyncio
async def test_balanced_runs_one_critique_and_second_gather():
    tools = FakeToolExecutor()
    planner = FakePlanner(_plan([_step()]))
    synthesizer = FakeSynthesizer("Done.")
    critic = FakeCritic(gap="Need newer numbers.")
    events = EventCollector()

    result = await run_deep_research(
        DeepResearchRequest(question="Q", depth="balanced", scope="web"),
        planner=planner,
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=critic,
        emit=events,
    )

    assert result.report == "Done."
    assert critic.calls == 1
    assert synthesizer.calls == 1
    # round 1: original step; round 2: original step + gap step
    assert len(tools.calls) == 3
    labels = [label or "" for _, label in events.events]
    assert labels.count("phase_enter.research_gather") == 2
    assert labels.count("phase_enter.research_critique") == 1
    # the critic's gap became the next round's tool query
    assert any(
        "Need newer numbers." in str(args.get("query", ""))
        for _, args in tools.calls
    )


# ── Orchestrator: deep depth ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_deep_runs_two_critique_rounds():
    tools = FakeToolExecutor()
    planner = FakePlanner(_plan([_step()]))
    synthesizer = FakeSynthesizer("Done.")
    critic = FakeCritic()

    await run_deep_research(
        DeepResearchRequest(question="Q", depth="deep", scope="web"),
        planner=planner,
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=critic,
        emit=EventCollector(),
    )

    assert critic.calls == 2
    assert len(tools.calls) >= 3  # round1: 1 step, rounds 2-3 add gap steps


# ── Scope filtering ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_scope_skips_internal_tools():
    tools = FakeToolExecutor()
    planner = FakePlanner(_plan([_step(tools=["web_search", "sql_query"])]))
    synthesizer = FakeSynthesizer("Done.")
    await run_deep_research(
        DeepResearchRequest(question="Q", depth="quick", scope="web"),
        planner=planner,
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    names = [n for n, _ in tools.calls]
    assert names == ["web_search"]
    assert "sql_query" not in names


@pytest.mark.asyncio
async def test_internal_scope_skips_web_tools():
    tools = FakeToolExecutor()
    planner = FakePlanner(_plan([_step(tools=["web_search", "sql_query"])]))
    synthesizer = FakeSynthesizer("Done.")
    await run_deep_research(
        DeepResearchRequest(question="Q", depth="quick", scope="internal"),
        planner=planner,
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    names = [n for n, _ in tools.calls]
    assert names == ["sql_query"]


@pytest.mark.asyncio
async def test_both_scope_runs_all_plan_tools():
    tools = FakeToolExecutor()
    planner = FakePlanner(_plan([_step(tools=["web_search", "sql_query"])]))
    synthesizer = FakeSynthesizer("Done.")
    await run_deep_research(
        DeepResearchRequest(question="Q", depth="quick", scope="both"),
        planner=planner,
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    names = sorted(n for n, _ in tools.calls)
    assert names == ["sql_query", "web_search"]


# ── Plan fallback ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_malformed_plan_falls_back_to_generic_step():
    tools = FakeToolExecutor()
    planner = FakePlanner({})  # planner returns junk
    synthesizer = FakeSynthesizer("Done.")
    await run_deep_research(
        DeepResearchRequest(question="Q", depth="quick", scope="web"),
        planner=planner,
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    # fallback step still drives a tool call
    assert len(tools.calls) == 1
    assert tools.calls[0][0] in SCOPE_TOOLS["web"]


# ── Citations on final report ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_citations_parsed_from_report():
    planner = FakePlanner(_plan([_step()]))
    synthesizer = FakeSynthesizer(
        "Market grew 5% [source: Q3 Report]. Demand rose [source: Q3 Report]."
    )
    result = await run_deep_research(
        DeepResearchRequest(question="Q", depth="quick", scope="web"),
        planner=planner,
        tool_executor=FakeToolExecutor(),
        synthesizer=synthesizer,
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    assert len(result.citations) == 2
    assert result.citations[0]["source_name"] == "Q3 Report"


# ── Flag default ──────────────────────────────────────────────────────

def test_deep_research_flag_defaults_off():
    from app.config import settings
    assert getattr(settings, "DEEP_RESEARCH_ENABLED", False) is False
