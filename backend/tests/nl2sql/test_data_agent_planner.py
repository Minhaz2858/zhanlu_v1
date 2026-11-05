"""Data Agent Planner — the OCI "Agent Planning" stage, explicit and bounded.

Pins: one planning call classifies intent, decomposes multi-part questions,
and decides SQL vs deterministic vs clarification; JSON embedded in prose is
parsed; ANY failure (bad JSON, exception, flag off, empty question) falls
back to None = today's behavior; and the wiring in _ask_data_agent turns a
clarification plan into the structured needs_clarification result WITHOUT
spending loop iterations.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services.db import data_agent_planner as dap
from app.services.db.data_agent_planner import plan_data_question


# ── parsing / classification ──────────────────────────────────────────────


def test_parses_multi_part_plan():
    async def fake_llm(messages, **kwargs):
        return json.dumps(
            {
                "intent": "multi_part",
                "sub_questions": ["total revenue last month", "top 5 products by revenue"],
                "strategy": "sql",
            }
        )

    import asyncio

    plan = asyncio.run(
        plan_data_question("revenue last month and the top products", llm_call=fake_llm)
    )
    assert plan is not None
    assert plan.intent == "multi_part"
    assert len(plan.sub_questions) == 2
    assert plan.strategy == "sql"
    assert not plan.needs_clarification


def test_parses_json_wrapped_in_prose():
    payload = (
        'Here is my analysis:\n```json\n{"intent": "single_question", '
        '"sub_questions": [], "strategy": "sql"}\n```\nDone.'
    )

    async def fake_llm(messages, **kwargs):
        return payload

    import asyncio

    plan = asyncio.run(
        plan_data_question("how did we do last month", llm_call=fake_llm)
    )
    assert plan is not None
    assert plan.intent == "single_question"


def test_clarification_plan_for_ambiguous_question():
    async def fake_llm(messages, **kwargs):
        return json.dumps(
            {
                "intent": "unclear",
                "sub_questions": [],
                "strategy": "sql",
                "needs_clarification": True,
                "clarification": {
                    "question": "Which metric do you mean by 'did'?",
                    "options": ["revenue", "margin", "bookings"],
                },
            }
        )

    import asyncio

    plan = asyncio.run(plan_data_question("how is OCI doing", llm_call=fake_llm))
    assert plan is not None
    assert plan.needs_clarification
    assert plan.clarification["options"][0] == "revenue"


def test_deterministic_strategy_recognised():
    async def fake_llm(messages, **kwargs):
        return json.dumps({"intent": "deterministic", "sub_questions": [], "strategy": "deterministic"})

    import asyncio

    plan = asyncio.run(plan_data_question("what is 15% of 300", llm_call=fake_llm))
    assert plan is not None
    assert plan.strategy == "deterministic"


# ── fail-soft contract ────────────────────────────────────────────────────


def test_garbage_response_falls_back_to_none():
    async def fake_llm(messages, **kwargs):
        return "I am not sure what you mean."

    import asyncio

    assert asyncio.run(plan_data_question("q", llm_call=fake_llm)) is None


def test_llm_exception_falls_back_to_none():
    async def fake_llm(messages, **kwargs):
        raise RuntimeError("upstream down")

    import asyncio

    assert asyncio.run(plan_data_question("q", llm_call=fake_llm)) is None


def test_empty_question_returns_none():
    import asyncio

    async def fake_llm(messages, **kwargs):  # must not be called
        raise AssertionError("planner must not call the LLM for an empty question")

    assert asyncio.run(plan_data_question("   ", llm_call=fake_llm)) is None


def test_flag_gates_the_planner(monkeypatch):
    monkeypatch.setattr(settings, "DATA_AGENT_PLANNER_ENABLED", False)

    async def fake_llm(messages, **kwargs):
        raise AssertionError("flag off must not call the LLM")

    import asyncio

    assert asyncio.run(plan_data_question("q", llm_call=fake_llm)) is None


# ── wiring in _ask_data_agent ─────────────────────────────────────────────


def test_ask_data_agent_returns_clarification_without_loop(monkeypatch):
    """A clarification plan short-circuits the sub-loop — no iterations spent."""
    import asyncio
    from app.services.tool_handlers import delegation_tools as dt

    monkeypatch.setattr(settings, "DATA_AGENT_PLANNER_ENABLED", True)
    monkeypatch.setattr(settings, "DATA_AGENT_FASTPATH_ENABLED", False)

    async def fake_plan(question, **kwargs):
        return dap.DataAgentPlan(
            intent="unclear",
            sub_questions=[],
            needs_clarification=True,
            clarification={
                "question": "Which metric do you mean?",
                "options": ["revenue", "margin"],
            },
        )

    monkeypatch.setattr(dap, "plan_data_question", fake_plan)

    async def boom(*a, **k):
        raise AssertionError("the sub-loop must not run after a clarification plan")

    monkeypatch.setattr(dt, "_build_sub_agent_prompt", boom)

    result = asyncio.run(
        dt._ask_data_agent(
            {"question": "how is OCI doing"}, db=None, user_id="u", context={}
        )
    )
    assert result.get("needs_clarification") is True
    assert result["clarification"]["options"] == ["revenue", "margin"]


def test_ask_data_agent_degrades_to_legacy_on_planner_failure(monkeypatch):
    """Planner raising must NOT change the legacy path (fail-soft wiring)."""
    import asyncio
    from app.services.tool_handlers import delegation_tools as dt

    monkeypatch.setattr(settings, "DATA_AGENT_PLANNER_ENABLED", True)
    monkeypatch.setattr(settings, "DATA_AGENT_FASTPATH_ENABLED", False)
    # Isolate the legacy fail-soft path: when the swarm sub-agent path is
    # enabled (default in some deployments) a non-None swarm result short-circuits
    # the legacy loop, so the planner-failure fallback under test would never
    # build the legacy prompt. Disable swarm to exercise exactly that wiring.
    monkeypatch.setattr(settings, "DATA_AGENT_SWARM_ENABLED", False)

    async def broken(*a, **k):
        raise RuntimeError("planner down")

    monkeypatch.setattr(dap, "plan_data_question", broken)

    # The legacy loop starts with the prompt builder; make it observable.
    calls = []

    def fake_prompt(question, bound, preferred, caller=None, db=None):
        calls.append(question)
        return "system"

    monkeypatch.setattr(dt, "_build_sub_agent_prompt", fake_prompt)

    async def fake_llm_retry(messages, tools, **kwargs):
        return {"content": "", "tool_calls": []}  # loop exits with final_text=""

    monkeypatch.setattr(dt, "_call_llm_with_retry", fake_llm_retry)

    result = asyncio.run(
        dt._ask_data_agent(
            {"question": "sales last month"}, db=None, user_id="u", context={}
        )
    )
    assert calls == ["sales last month"], "legacy prompt build must still happen"
    assert "success" in result
