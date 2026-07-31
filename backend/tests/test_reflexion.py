"""Tests for the reflexion LLM-rubric pass."""

import asyncio
import os

import pytest

from app.services.synexia.reflexion import (
    ReflexionVerdict,
    _extract_json,
    _fallback_verdict,
    _strip_code_fences,
    critique,
)


def test_strip_code_fences_removes_fences():
    assert _strip_code_fences("```json\n{}\n```") == "{}"
    assert _strip_code_fences("  ```\n{}\n```  ") == "{}"
    assert _strip_code_fences("```\n{}\n```") == "{}"


def test_extract_json_handles_padded_response():
    assert _extract_json('Sure here: {"verdict": "accept"}') == {"verdict": "accept"}
    assert _extract_json("```json\n{\"verdict\": \"revise\"}\n```") == {
        "verdict": "revise"
    }
    assert _extract_json("not json") is None


def test_fallback_verdict_detects_failure_markers():
    v = _fallback_verdict("Failed to load artifact: HTTP 404")
    assert v.verdict == "revise"
    assert v.confidence < 0.5


def test_fallback_verdict_accepts_clean_text():
    v = _fallback_verdict("Here is the report you asked for.")
    assert v.verdict == "accept"


def test_critique_uses_heuristic_when_disabled(monkeypatch):
    monkeypatch.setenv("SYNEXIA_VERIFIER_LLM_ENABLED", "0")

    async def should_not_call(messages):
        raise AssertionError("llm_call should not be invoked when disabled")

    out = asyncio.run(
        critique(
            user_message="make a report",
            assistant_text="Here is the report.",
            llm_call=should_not_call,
        )
    )
    assert isinstance(out, ReflexionVerdict)
    assert out.verdict == "accept"


def test_critique_handles_malformed_llm_output(monkeypatch):
    monkeypatch.setenv("SYNEXIA_VERIFIER_LLM_ENABLED", "1")

    async def garbage(_):
        return "this is not json"

    out = asyncio.run(
        critique(
            user_message="x",
            assistant_text="Here is the report.",
            llm_call=garbage,
        )
    )
    # Heuristic fallback kicks in when the JSON parse fails.
    assert out.verdict in ("accept", "revise", "reject")


def test_critique_parses_valid_llm_output(monkeypatch):
    monkeypatch.setenv("SYNEXIA_VERIFIER_LLM_ENABLED", "1")

    async def good(_):
        return json.dumps({
            "verdict": "revise",
            "confidence": 0.42,
            "issues": ["summary is missing"],
            "suggestions": ["add a one-line TL;DR"],
        })

    import json

    out = asyncio.run(
        critique(
            user_message="x",
            assistant_text="Here is the report.",
            llm_call=good,
        )
    )
    assert out.verdict == "revise"
    assert out.confidence == pytest.approx(0.42)
    assert "summary is missing" in out.issues


def test_critique_clamps_confidence(monkeypatch):
    monkeypatch.setenv("SYNEXIA_VERIFIER_LLM_ENABLED", "1")

    async def crazy(_):
        return json.dumps({"verdict": "accept", "confidence": 99.0})

    out = asyncio.run(
        critique(
            user_message="x",
            assistant_text="y",
            llm_call=crazy,
        )
    )
    assert 0.0 <= out.confidence <= 1.0
