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
    # 2026-09-03: internal-family tools funnel through the NL data agent —
    # a research step only has a *question*, not SQL, so sql_query is
    # invoked as ask_data_agent (the gateway that resolves the question
    # against the bound warehouse). Web tools stay filtered out.
    assert names == ["ask_data_agent"]
    assert "web_search" not in names


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
    # 2026-09-03: sql_query funnels through the NL data agent (research steps
    # carry a question, not SQL), so both families still run but internal is
    # invoked as ask_data_agent.
    assert names == ["ask_data_agent", "web_search"]


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
    # Class-level default is OFF even if an operator has enabled it via env
    # (root .env / compose) in this deployment.
    from app.config import Settings
    assert Settings.model_fields["DEEP_RESEARCH_ENABLED"].default is False


# ── Hybrid scope=both regression (2026-09-03) ─────────────────────────
# User-visible bug: scope=both silently degraded to web-only; a web-only
# plan for an internal product question (C5C9 sales/forecast) never touched
# the bound warehouse and produced a "no data exists" report. These tests
# pin the three fixes:
#   B1: failure dicts are NOT evidence (they became "findings" pre-fix)
#   B2: internal tools are called with {"question": ...}, web with {"query": ...}
#   B3: scope=both supplements the missing family before synthesis

class FailingWebToolExecutor:
    """web_search returns permanent failure; ask_data_agent returns rows."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "web_search":
            return {
                "success": False,
                "failure_kind": "permanent",
                "error": "web_search failed across providers: provider returned no results",
            }
        if name == "ask_data_agent":
            return {
                "success": True,
                "answer": "C5C9 sales for Aug 24-30: 7 date rows, 73.47M CNY.",
                "rows": [{"date": "2026-08-24"}],
                "sql": "SELECT ... FROM erp_v_sale_orderentry ...",
            }
        return {"success": False, "error": "unsupported tool"}


class WebOnlyToolExecutor:
    """web_search SUCCEEDS; used to prove no internal supplement is forced
    when web already produced evidence (web-only scope stays web-only)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "web_search":
            return {"rows": [{"title": "public result"}], "url": "https://x.io"}
        return {"success": False, "error": "unsupported"}


@pytest.mark.asyncio
async def test_scope_both_supplements_internal_when_web_fails():
    """The C5C9 repro: web-only plan, web fails permanently → the internal
    data agent MUST still be consulted before finalize."""
    tools = FailingWebToolExecutor()
    planner = FakePlanner(_plan([_step(q="C5C9 weekly summary?", tools=["web_search"])]))
    events = EventCollector()

    result = await run_deep_research(
        DeepResearchRequest(question="C5C9 weekly summary and forecast Aug 24-30?",
                            depth="quick", scope="both"),
        planner=planner,
        tool_executor=tools,
        synthesizer=FakeSynthesizer("Hybrid report."),
        critic=FakeCritic(),
        emit=events,
    )

    names = [n for n, _ in tools.calls]
    # web_search attempted by the plan step; internal supplement MUST run
    assert "web_search" in names
    assert "ask_data_agent" in names
    # ask_data_agent received the REAL arg schema (question), not {"query": ...}
    ask_args = [a for n, a in tools.calls if n == "ask_data_agent"]
    assert ask_args and "question" in ask_args[0]
    assert result.report == "Hybrid report."


@pytest.mark.asyncio
async def test_failure_dicts_are_not_evidence():
    """A permanent web failure must not reach the synthesizer as a finding."""
    tools = FailingWebToolExecutor()
    synthesizer = FakeSynthesizer("Report.")
    await run_deep_research(
        DeepResearchRequest(question="C5C9?", depth="quick", scope="both"),
        planner=FakePlanner(_plan([_step(tools=["web_search"])])),
        tool_executor=tools,
        synthesizer=synthesizer,
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    user_msgs = [m for m in synthesizer.last_messages if m["role"] == "user"]
    evidence_text = user_msgs[0]["content"] if user_msgs else ""
    # the failure text must NOT appear as evidence content
    assert "failed across providers" not in evidence_text
    # the successful internal answer IS present
    assert "C5C9 sales" in evidence_text


@pytest.mark.asyncio
async def test_scope_web_with_success_stays_web_only():
    """scope=web: successful web evidence means NO internal supplement —
    scope isolation is preserved (web scope must not touch the warehouse)."""
    tools = WebOnlyToolExecutor()
    await run_deep_research(
        DeepResearchRequest(question="Market context?", depth="quick", scope="web"),
        planner=FakePlanner(_plan([_step(tools=["web_search"])])),
        tool_executor=tools,
        synthesizer=FakeSynthesizer("Report."),
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    names = [n for n, _ in tools.calls]
    assert "ask_data_agent" not in names


@pytest.mark.asyncio
async def test_scope_both_supplements_web_when_internal_only_plan():
    """Mirror case: DB-only plan for a market question → web supplement runs."""
    tools = WebOnlyToolExecutor()

    class InternalOnlyToolExecutor:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        async def __call__(self, name, args):
            self.calls.append((name, args))
            if name == "ask_data_agent":
                return {"success": True, "answer": "warehouse rows", "rows": [{"k": 1}]}
            if name == "web_search":
                return {"rows": [{"title": "public"}], "url": "https://x.io"}
            return {"success": False, "error": "unsupported"}

    tools = InternalOnlyToolExecutor()
    await run_deep_research(
        DeepResearchRequest(question="Hybrid question?", depth="quick", scope="both"),
        planner=FakePlanner(_plan([_step(tools=["ask_data_agent"])])),
        tool_executor=tools,
        synthesizer=FakeSynthesizer("Report."),
        critic=FakeCritic(),
        emit=EventCollector(),
    )
    names = [n for n, _ in tools.calls]
    assert "ask_data_agent" in names
    assert "web_search" in names  # supplement added the missing web family
