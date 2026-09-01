"""Tests for Phase 5 — LLM copy-polish pass.

Covers the polish prompt, the structural application of the LLM's tightened
copy, and the failure/timeout fallbacks (no live LLM in CI).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.services.artifacts.copy_polish import (
    _apply_polish,
    _build_polish_prompt,
    polish_deck,
)
from app.services.synexia.contracts import (
    ChartSpecInSlide,
    DeckPlan,
    SlidePlan,
)


def _plan() -> DeckPlan:
    return DeckPlan(
        title="Q3 Sales",
        deck_type="data_report",
        slides=[
            SlidePlan(
                layout="cover",
                title="Q3 Sales Overview",
                subtitle="a long subtitle",
            ),
            SlidePlan(
                layout="insights_bullets",
                title="Key Insights",
                bullets=["revenue grew strongly this quarter"],
            ),
        ],
    )


class TestPolishPrompt:
    def test_prompt_contains_titles_and_bullets(self):
        prompt = _build_polish_prompt(_plan(), "make it tight")
        assert "Q3 Sales Overview" in prompt
        assert "revenue grew strongly this quarter" in prompt
        assert "at most 8 words" in prompt
        assert "at most 12 words" in prompt

    def test_prompt_mentions_speaker_notes(self):
        prompt = _build_polish_prompt(_plan(), "x")
        assert "speaker notes" in prompt


class TestApplyPolish:
    def test_apply_polish_rewrites_text(self):
        data = {
            "title": "Q3 Sales",
            "slides": [
                {"title": "Overview", "subtitle": "s", "bullets": ["b"], "notes": "n"},
                {"title": "Insights", "bullets": ["tightened"], "notes": "n2"},
            ],
        }
        out = _apply_polish(_plan(), data)
        assert out.slides[0].title == "Overview"
        assert out.slides[1].bullets == ["tightened"]
        # structure preserved — only text changes
        assert out.slides[0].layout == "cover"
        assert out.slides[1].layout == "insights_bullets"

    def test_apply_polish_slide_count_mismatch_returns_original(self):
        plan = _plan()
        out = _apply_polish(plan, {"title": "X", "slides": [{"title": "only one"}]})
        assert out is plan

    def test_apply_polish_preserves_chart_spec(self):
        plan = DeckPlan(
            title="T",
            slides=[
                SlidePlan(
                    layout="chart_full",
                    title="C",
                    chart_spec=ChartSpecInSlide(
                        chart_type="bar", x_key="m", y_keys=["r"], title="R"
                    ),
                )
            ],
        )
        out = _apply_polish(
            plan, {"title": "T", "slides": [{"title": "New", "notes": "n"}]}
        )
        assert out.slides[0].chart_spec.chart_type == "bar"
        assert out.slides[0].chart_spec.x_key == "m"

    def test_apply_polish_ignores_non_dict_slide(self):
        plan = _plan()
        out = _apply_polish(
            plan, {"title": "T", "slides": [None, {"title": "New", "notes": "n"}]}
        )
        # first slide kept as original (non-dict item), second rewritten
        assert out.slides[0].title == "Q3 Sales Overview"
        assert out.slides[1].title == "New"


class TestPolishDeck:
    def test_polish_deck_no_slides_returns_unchanged(self):
        plan = DeckPlan(title="T")

        async def _run():
            return await polish_deck(plan, [], "x")

        assert asyncio.run(_run()) is plan

    def test_polish_deck_llm_failure_returns_unchanged(self):
        plan = _plan()

        async def _boom(*a, **k):
            raise RuntimeError("llm down")

        async def _run():
            with patch("app.services.llm_service.call_llm", side_effect=_boom):
                return await polish_deck(plan, [], "x")

        assert asyncio.run(_run()) is plan

    def test_polish_deck_success(self):
        plan = _plan()

        async def _fake_llm(*a, **k):
            return {
                "data": {
                    "title": "Q3 Sales",
                    "slides": [
                        {"title": "Overview", "bullets": [], "notes": "n"},
                        {"title": "Insights", "bullets": ["tight"], "notes": "n"},
                    ],
                }
            }

        async def _run():
            with patch("app.services.llm_service.call_llm", side_effect=_fake_llm):
                return await polish_deck(plan, [], "x")

        out = asyncio.run(_run())
        assert out.slides[0].title == "Overview"
        assert out.slides[1].bullets == ["tight"]
        # deck_type / theme preserved through model_copy
        assert out.deck_type == "data_report"

    def test_polish_deck_non_dict_output_returns_unchanged(self):
        plan = _plan()

        async def _fake_llm(*a, **k):
            return {"data": ["not", "a", "dict"]}

        async def _run():
            with patch("app.services.llm_service.call_llm", side_effect=_fake_llm):
                return await polish_deck(plan, [], "x")

        assert asyncio.run(_run()) is plan
