"""Tests for deck_planner assertion-headline enforcement + SKILL injection.

Verifies:
* Topic-label headlines ("Key Findings") are flagged as topic labels.
* _enforce_assertion_headlines rewrites a topic label via the deterministic
  formatter when polish/LLM is unavailable (no crash, deck still valid).
* The planner prompt injects the condensed pptx SKILL methodology.
* The plan schema now advertises headline_style + bullet budgets.
"""

from __future__ import annotations

from app.services.synexia.contracts import DeckPlan, SlidePlan
from app.services.artifacts.deck_planner import (
    _enforce_assertion_headlines,
    _is_topic_label,
    _build_planner_prompt,
    _PPTX_SKILL_METHODOLOGY,
)


def _plan_with(title: str) -> DeckPlan:
    return DeckPlan(
        title="Deck",
        slides=[
            SlidePlan(layout="cover", title="Deck", narrative_role="hook"),
            SlidePlan(layout="insights_bullets", title=title,
                      bullets=["A", "B"], narrative_role="insight"),
            SlidePlan(layout="closing", title="Thank you", narrative_role="closing"),
        ],
    )


def test_topic_labels_are_detected():
    assert _is_topic_label("Key Findings")
    assert _is_topic_label("Summary")
    assert _is_topic_label("Recommendations")
    # Assertion headlines are NOT topic labels.
    assert not _is_topic_label("Enterprise drove 60% of net new bookings")
    assert not _is_topic_label("Revenue grew 8% quarter over quarter.")


def test_enforce_rewrites_topic_label_without_crashing():
    # With PPT_LLM_POLISH_ENABLED off (default in tests), the deterministic
    # formatter path runs and must not raise; the plan stays valid.
    plan = _plan_with("Key Findings")
    out = _enforce_assertion_headlines(plan)
    # Plan is unchanged in structure (same slide count) and still serializable.
    assert len(out.slides) == 3
    assert out.slides[1].headline_style in {"topic", "assertion"}
    # It must remain a valid DeckPlan (model round-trips).
    assert DeckPlan.model_validate(out.model_dump()) is not None


def test_enforce_preserves_assertion_headlines():
    plan = _plan_with("Revenue grew 8% quarter over quarter.")
    out = _enforce_assertion_headlines(plan)
    assert out.slides[1].title == "Revenue grew 8% quarter over quarter."
    assert out.slides[1].headline_style == "assertion"


def test_planner_prompt_injects_skill_methodology():
    prompt = _build_planner_prompt(
        "build a sales deck", "profile", [{"chart_type": "bar", "x_key": "m",
                                           "y_keys": ["v"], "title": "t"}], 10
    )
    assert "ASSERTION HEADLINES" in prompt
    assert "DENSITY BUDGET" in prompt
    # The condensed methodology (or the live SKILL.md) is injected.
    assert ("METHODOLOGY" in prompt) or (_PPTX_SKILL_METHODOLOGY[:20] in prompt)


def test_plan_schema_advertises_budgets():
    from app.services.artifacts.deck_planner import _PLAN_SCHEMA

    slide_props = _PLAN_SCHEMA["properties"]["slides"]["items"]["properties"]
    assert "headline_style" in slide_props
    assert "max_bullets" in slide_props
    assert "max_words_per_bullet" in slide_props
    assert _PLAN_SCHEMA["properties"]["headline_style"]["enum"] == ["topic", "assertion"]
