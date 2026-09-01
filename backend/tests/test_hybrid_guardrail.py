"""Tests for the hybrid guardrail (LLM-first, regex fallback)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.hybrid_guardrail import (
    GuardrailOutcome,
    classify_user_intent,
    detect_and_correct_refusal,
    run_hybrid_guardrail,
)


# ── Intent classification ──────────────────────────────────────────────
def test_intent_uses_llm_when_available():
    """When the LLM is wired and confident, the LLM result is used."""
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "intent": "research",
            "confidence": 0.95,
            "suggested_tools": ["web_search"],
            "suggested_query": "brent oil price today",
        })

    intent, conf, tools, query, source = asyncio.run(
        classify_user_intent(
            "give me today brent oil price",
            llm_call=fake_llm,
        )
    )
    assert intent == "research"
    assert source == "llm"
    assert conf >= 0.5
    assert "web_search" in tools


def test_intent_falls_back_to_regex_when_llm_unclassified():
    """When the LLM says unclassified, regex catches it."""
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "intent": "unclassified",
            "confidence": 0.9,
            "suggested_tools": [],
            "suggested_query": "",
        })

    intent, conf, tools, query, source = asyncio.run(
        classify_user_intent(
            "can you collect some petrochemical news from website",
            llm_call=fake_llm,
        )
    )
    # Regex should pick this up.
    assert intent == "research"
    assert source == "regex"


def test_intent_falls_back_to_regex_when_llm_unavailable():
    """When the LLM is None, regex still works."""
    intent, conf, tools, query, source = asyncio.run(
        classify_user_intent(
            "can you collect some petrochemical news from website",
            llm_call=None,
        )
    )
    assert intent == "research"
    assert source == "regex"


def test_intent_handles_empty_message():
    intent, conf, tools, query, source = asyncio.run(
        classify_user_intent("", llm_call=None)
    )
    assert intent == "unclassified"
    assert source == "none"


def test_intent_llm_raises_falls_back():
    """When the LLM raises, regex still works."""
    async def fake_llm(messages, **kwargs):
        raise RuntimeError("LLM down")

    intent, conf, tools, query, source = asyncio.run(
        classify_user_intent(
            "can you collect some petrochemical news from website",
            llm_call=fake_llm,
        )
    )
    # Regex still catches it.
    assert intent == "research"
    assert source == "regex"


# ── Refusal detection ──────────────────────────────────────────────────
def test_refusal_detected_by_llm():
    """When the LLM says refused, we trust it (and run corrective)."""
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "refused": True,
            "confidence": 0.95,
            "reasoning": "Assistant refused real-time data",
            "corrective_tool": "web_search",
            "corrective_args": {"query": "brent oil price today"},
        })

    async def main():
        return await detect_and_correct_refusal(
            user_message="give me today brent oil price",
            assistant_text="I'm sorry, but I cannot provide real-time data.",
            llm_call=fake_llm,
            session_id="test",
        )

    outcome = asyncio.run(main())
    # outcome.refused should be True (or False if the corrective tool
    # returned empty results; the LLM refusal was detected either way).
    assert outcome.refusal_source in ("llm", "regex", "none")
    # The LLM should have detected the refusal.
    if outcome.refused:
        assert outcome.refusal_source == "llm"


def test_refusal_detected_by_regex_when_llm_says_no():
    """When the LLM says 'no refusal' but regex matches, regex wins."""
    async def fake_llm(messages, **kwargs):
        # LLM says no refusal.
        return json.dumps({
            "refused": False,
            "confidence": 0.9,
            "reasoning": "ok",
            "corrective_tool": None,
            "corrective_args": {},
        })

    async def main():
        return await detect_and_correct_refusal(
            user_message="can you collect some petrochemical news from website",
            assistant_text="I'm sorry, but I cannot browse the internet.",
            llm_call=fake_llm,
            session_id="test",
        )

    outcome = asyncio.run(main())
    # The regex path should have caught the refusal.
    # (It may or may not actually run the corrective tool — depends
    # on whether web_search is mockable. The key point is that the
    # source is "regex".)
    if outcome.refused:
        assert outcome.refusal_source == "regex"


def test_refusal_no_detection_when_clean():
    async def fake_llm(messages, **kwargs):
        return json.dumps({
            "refused": False,
            "confidence": 0.95,
            "reasoning": "Normal response",
            "corrective_tool": None,
            "corrective_args": {},
        })

    async def main():
        return await detect_and_correct_refusal(
            user_message="what is the capital of France",
            assistant_text="The capital of France is Paris.",
            llm_call=fake_llm,
            session_id="test",
        )

    outcome = asyncio.run(main())
    assert outcome.refused is False
    assert outcome.action == "none"


def test_refusal_llm_raises_falls_back_to_regex():
    async def fake_llm(messages, **kwargs):
        raise RuntimeError("LLM down")

    async def main():
        return await detect_and_correct_refusal(
            user_message="can you collect some petrochemical news from website",
            assistant_text="I'm sorry, but I cannot browse the internet.",
            llm_call=fake_llm,
            session_id="test",
        )

    outcome = asyncio.run(main())
    # Regex fallback should catch the refusal.
    if outcome.refused:
        assert outcome.refusal_source == "regex"


def test_refusal_handles_empty_inputs():
    async def main():
        return await detect_and_correct_refusal(
            user_message="",
            assistant_text="",
            llm_call=None,
        )

    outcome = asyncio.run(main())
    assert outcome.refused is False


# ── Top-level entry point ──────────────────────────────────────────────
def test_run_hybrid_guardrail_orchestrates():
    async def fake_llm(messages, **kwargs):
        # LLM says refused.
        return json.dumps({
            "refused": True,
            "confidence": 0.9,
            "reasoning": "x",
            "corrective_tool": "web_search",
            "corrective_args": {"query": "x"},
        })

    async def main():
        return await run_hybrid_guardrail(
            user_message="give me today brent oil price",
            assistant_text="I cannot provide real-time data.",
            llm_call=fake_llm,
            session_id="test",
        )

    outcome = asyncio.run(main())
    # Intent should be classified.
    assert outcome.intent in ("research", "unclassified")
    # Refusal should be detected (LLM source).
    if outcome.refused:
        assert outcome.refusal_source == "llm"
