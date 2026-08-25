"""Tests for deck-planner personalization — brand + audience context in the prompt.

The planner was blind to the user's brand kit, role, and preferences.  These
tests lock in that ``theme_tokens`` / ``personalization`` reach the prompt as
BRAND STYLE / AUDIENCE & CONTEXT guidance, and that the new keyword-only params
do not break the historical ``build_deck_plan("intent", rows)`` call shape.
"""

import asyncio

import pytest


def test_prompt_includes_brand_style():
    from app.services.artifacts.deck_planner import _build_planner_prompt

    prompt = _build_planner_prompt(
        "sales", "data summary", [], 10,
        theme_tokens={
            "primary": "#7c3aed",
            "chart_palette": ["#7c3aed", "#1d4ed8", "#0ea5e9"],
            "fonts": {"heading": "Inter", "body": "Arial"},
        },
    )
    assert "BRAND STYLE" in prompt
    assert "#7c3aed" in prompt
    assert "Inter" in prompt


def test_prompt_includes_audience_context():
    from app.services.artifacts.deck_planner import _build_planner_prompt

    prompt = _build_planner_prompt(
        "sales", "data", [], 10,
        personalization={
            "role_text": "R runs weekly P&L reporting.",
            "profile_text": "Preferred language: English",
            "brand_name": "Acme",
        },
    )
    assert "AUDIENCE" in prompt
    assert "P&L" in prompt
    assert "Preferred language: English" in prompt


def test_prompt_without_context_is_unchanged():
    from app.services.artifacts.deck_planner import _build_planner_prompt

    prompt = _build_planner_prompt("sales", "data", [], 10)
    assert "BRAND STYLE" not in prompt
    assert "AUDIENCE" not in prompt
    assert "USER INTENT" in prompt


def test_build_deck_plan_accepts_personalization_keywords(monkeypatch):
    monkeypatch.setattr("app.config.settings.PPT_DECK_PLANNER_ENABLED", False)
    from app.services.artifacts.deck_planner import build_deck_plan

    async def _run():
        return await build_deck_plan(
            "Executive sales report",
            [{"month": "2025-01", "revenue": 100}],
            theme_tokens={"primary": "#111111"},
            user_context={"role_text": "CFO"},
        )

    plan, profile = asyncio.run(_run())
    assert plan.slides[0].layout == "cover"
    assert plan.slides[-1].layout == "closing"
    assert profile
