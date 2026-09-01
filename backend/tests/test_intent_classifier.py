"""Tests for the LLM-based intent classifier."""

import asyncio
import json

import pytest

from app.services.intent_classifier import (
    Intent,
    IntentClassifier,
    IntentResult,
    classify_intent,
)


def test_intent_result_round_trip():
    r = IntentResult(
        intent=Intent.RESEARCH,
        confidence=0.92,
        suggested_tools=["web_search"],
        suggested_query="brent oil price today",
        reasoning="real-time",
    )
    d = r.to_dict()
    assert d["intent"] == "research"
    assert d["confidence"] == 0.92
    json.dumps(d)  # must be serializable


def test_intent_enum_categories():
    assert Intent.RESEARCH.value == "research"
    assert Intent.FILE_GENERATION.value == "file_generation"
    assert Intent.DATA_QUERY.value == "data_query"
    assert Intent.CHITCHAT.value == "chitchat"
    assert Intent.UNCLASSIFIED.value == "unclassified"


def test_parses_valid_json():
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "intent": "research",
            "confidence": 0.95,
            "suggested_tools": ["web_search"],
            "suggested_query": "brent oil price today",
            "reasoning": "real-time price",
        })

    out = asyncio.run(classify_intent("give me today brent oil price", llm_call=fake_llm))
    assert out.intent == Intent.RESEARCH
    assert out.confidence == 0.95
    assert "web_search" in out.suggested_tools


def test_strips_markdown_code_fence():
    async def fake_llm(messages, **kwargs):
        return "```json\n" + json.dumps({
            "intent": "research",
            "confidence": 0.9,
            "suggested_tools": ["web_search"],
            "suggested_query": "AI news",
        }) + "\n```"

    out = asyncio.run(classify_intent("look up AI news", llm_call=fake_llm))
    assert out.intent == Intent.RESEARCH


def test_garbage_falls_back_to_unclassified():
    async def fake_llm(messages, **kwargs):
        return "I don't understand."

    out = asyncio.run(classify_intent("hello", llm_call=fake_llm))
    assert out.intent == Intent.UNCLASSIFIED
    assert out.confidence == 0.0
    assert out.suggested_tools == []


def test_llm_raises_falls_back():
    async def fake_llm(messages, **kwargs):
        raise RuntimeError("LLM down")

    out = asyncio.run(classify_intent("hello", llm_call=fake_llm))
    assert out.intent == Intent.UNCLASSIFIED


def test_clamps_confidence():
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "intent": "research",
            "confidence": 99.0,
            "suggested_tools": ["web_search"],
            "suggested_query": "x",
        })

    out = asyncio.run(classify_intent("test", llm_call=fake_llm))
    assert 0.0 <= out.confidence <= 1.0


def test_unknown_intent_string_normalized():
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "intent": "frobnicate",
            "confidence": 0.5,
            "suggested_tools": ["web_search"],
            "suggested_query": "x",
        })

    out = asyncio.run(classify_intent("test", llm_call=fake_llm))
    assert out.intent == Intent.UNCLASSIFIED


def test_prompt_contains_user_message_and_schema():
    captured = {}

    async def fake_llm(messages, **kwargs):
        captured["messages"] = messages
        return json.dumps({
            "intent": "research",
            "confidence": 0.8,
            "suggested_tools": ["web_search"],
            "suggested_query": "x",
        })

    asyncio.run(classify_intent("USER_INPUT_XYZ", llm_call=fake_llm))
    user_msgs = [m for m in captured["messages"] if m.get("role") == "user"]
    sys_msgs = [m for m in captured["messages"] if m.get("role") == "system"]
    assert any("USER_INPUT_XYZ" in m["content"] for m in user_msgs)
    assert any("intent" in m["content"] for m in sys_msgs)


def test_caches_per_session():
    calls = {"n": 0}

    async def fake_llm(messages, **kwargs):
        calls["n"] += 1
        return json.dumps({
            "intent": "research",
            "confidence": 0.9,
            "suggested_tools": ["web_search"],
            "suggested_query": "x",
        })

    classifier = IntentClassifier(llm_call=fake_llm, enable_cache=True)
    msg = "give me today brent oil price"
    asyncio.run(classifier.classify(msg, session_id="s1"))
    asyncio.run(classifier.classify(msg, session_id="s1"))  # cached
    asyncio.run(classifier.classify(msg, session_id="s2"))  # different session
    assert calls["n"] == 2


def test_cache_disabled_always_calls_llm():
    calls = {"n": 0}

    async def fake_llm(messages, **kwargs):
        calls["n"] += 1
        return json.dumps({
            "intent": "research",
            "confidence": 0.9,
            "suggested_tools": ["web_search"],
            "suggested_query": "x",
        })

    classifier = IntentClassifier(llm_call=fake_llm, enable_cache=False)
    msg = "test"
    asyncio.run(classifier.classify(msg, session_id="s1"))
    asyncio.run(classifier.classify(msg, session_id="s1"))
    assert calls["n"] == 2
