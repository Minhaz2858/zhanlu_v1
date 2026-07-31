"""Tests for the LLM-based self-critic (refusal detection + correction)."""

import asyncio
import json

import pytest

from app.services.self_critic import (
    CriticVerdict,
    SelfCritic,
    SelfCriticDecision,
    critique_response,
)


def test_critic_verdict_default_unrefused():
    v = CriticVerdict()
    assert v.refused is False
    assert v.confidence == 0.0
    assert v.corrective_tool is None


def test_self_critic_decision_round_trip():
    d = SelfCriticDecision(
        refused=True,
        confidence=0.92,
        reasoning="assistant refused real-time data",
        corrective_tool="web_search",
        corrective_args={"query": "brent oil price today"},
    )
    j = json.dumps(d.to_dict())
    assert "web_search" in j
    assert "refused" in j


def test_critic_parses_valid_refusal():
    """LLM correctly identifies a refusal."""
    llm_response = json.dumps({
        "refused": True,
        "confidence": 0.95,
        "reasoning": "Assistant claims it cannot provide real-time data",
        "corrective_tool": "web_search",
        "corrective_args": {"query": "brent oil price today"},
    })

    async def fake_llm(messages, **kwargs):
        return llm_response

    critic = SelfCritic(llm_call=fake_llm)
    decision = asyncio.run(critic.critique(
        user_message="give me today brent oil price",
        assistant_text="I'm sorry, but I cannot provide real-time data.",
    ))
    assert decision.refused is True
    assert decision.confidence == 0.95
    assert decision.corrective_tool == "web_search"
    assert decision.corrective_args["query"] == "brent oil price today"


def test_critic_parses_valid_no_refusal():
    """LLM correctly identifies a normal response."""
    llm_response = json.dumps({
        "refused": False,
        "confidence": 0.9,
        "reasoning": "Assistant answered normally",
        "corrective_tool": None,
        "corrective_args": {},
    })

    async def fake_llm(messages, **kwargs):
        return llm_response

    critic = SelfCritic(llm_call=fake_llm)
    decision = asyncio.run(critic.critique(
        user_message="what is the capital of France",
        assistant_text="The capital of France is Paris.",
    ))
    assert decision.refused is False


def test_critic_handles_code_fence():
    llm_response = "```json\n" + json.dumps({
        "refused": True,
        "confidence": 0.9,
        "reasoning": "refused",
        "corrective_tool": "web_search",
        "corrective_args": {"query": "x"},
    }) + "\n```"

    async def fake_llm(messages, **kwargs):
        return llm_response

    critic = SelfCritic(llm_call=fake_llm)
    decision = asyncio.run(critic.critique("u", "a"))
    assert decision.refused is True


def test_critic_falls_back_to_no_refusal_on_garbage():
    async def fake_llm(messages, **kwargs):
        return "I am confused."

    critic = SelfCritic(llm_call=fake_llm)
    decision = asyncio.run(critic.critique("u", "a"))
    assert decision.refused is False
    assert decision.confidence == 0.0


def test_critic_falls_back_when_llm_raises():
    async def fake_llm(messages, **kwargs):
        raise RuntimeError("LLM down")

    critic = SelfCritic(llm_call=fake_llm)
    decision = asyncio.run(critic.critique("u", "a"))
    assert decision.refused is False


def test_critic_clamps_confidence():
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "refused": True,
            "confidence": -5.0,
            "reasoning": "x",
            "corrective_tool": "web_search",
            "corrective_args": {"query": "x"},
        })

    critic = SelfCritic(llm_call=fake_llm)
    decision = asyncio.run(critic.critique("u", "a"))
    assert 0.0 <= decision.confidence <= 1.0


def test_critic_validates_args_type():
    """Args must be a dict."""
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "refused": True,
            "confidence": 0.9,
            "reasoning": "x",
            "corrective_tool": "web_search",
            "corrective_args": "not a dict",
        })

    critic = SelfCritic(llm_call=fake_llm)
    decision = asyncio.run(critic.critique("u", "a"))
    assert decision.corrective_args == {}


def test_critic_prompt_includes_both_messages():
    captured = {}

    async def fake_llm(messages, **kwargs):
        captured["messages"] = messages
        return json.dumps({
            "refused": False,
            "confidence": 0.9,
            "reasoning": "x",
            "corrective_tool": None,
            "corrective_args": {},
        })

    critic = SelfCritic(llm_call=fake_llm)
    asyncio.run(critic.critique(
        user_message="USER_QUERY_123",
        assistant_text="ASSISTANT_REPLY_456",
    ))
    msgs = captured["messages"]
    full_text = " ".join(m.get("content", "") for m in msgs)
    assert "USER_QUERY_123" in full_text
    assert "ASSISTANT_REPLY_456" in full_text


def test_critic_caches_per_session():
    calls = {"n": 0}

    async def fake_llm(messages, **kwargs):
        calls["n"] += 1
        return json.dumps({
            "refused": True,
            "confidence": 0.9,
            "reasoning": "x",
            "corrective_tool": "web_search",
            "corrective_args": {"query": "x"},
        })

    critic = SelfCritic(llm_call=fake_llm, enable_cache=True)
    user_msg = "give me today brent oil price"
    asst_msg = "I cannot provide real-time data."
    asyncio.run(critic.critique(user_msg, asst_msg, session_id="s1"))
    asyncio.run(critic.critique(user_msg, asst_msg, session_id="s1"))  # cached
    asyncio.run(critic.critique(user_msg, asst_msg, session_id="s2"))
    assert calls["n"] == 2


def test_critique_response_convenience_function():
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "refused": True,
            "confidence": 0.9,
            "reasoning": "x",
            "corrective_tool": "web_search",
            "corrective_args": {"query": "x"},
        })

    out = asyncio.run(critique_response(
        user_message="u",
        assistant_text="I cannot.",
        llm_call=fake_llm,
    ))
    assert out.refused is True
