"""Phase 4 — Deck profile definitions and classification."""

from __future__ import annotations

import pytest

from app.services.artifacts.deck_profiles import (
    ALL_PROFILES,
    DEFAULT_PROFILE,
    classify_profile,
    get_profile,
)


def test_four_profiles_defined():
    assert set(ALL_PROFILES) == {
        "data_report",
        "executive_brief",
        "pitch_narrative",
        "periodic_review",
    }


def test_profile_constraints_present():
    for p in ALL_PROFILES.values():
        assert 1 <= p.slide_count_range[0] <= p.slide_count_range[1]
        assert p.allowed_layouts
        assert p.preferred_chart_types
        assert p.mood_words


def test_executive_brief_forbids_tables():
    assert ALL_PROFILES["executive_brief"].forbid_tables is True
    assert ALL_PROFILES["data_report"].forbid_tables is False


def test_get_profile_falls_back_to_default():
    assert get_profile("nonsense") is DEFAULT_PROFILE
    assert get_profile("pitch_narrative").name == "pitch_narrative"


@pytest.mark.parametrize("intent,expected", [
    ("give me an executive summary for the board", "executive_brief"),
    ("create an investor pitch deck to raise our seed round", "pitch_narrative"),
    ("build a weekly status review of shipments", "periodic_review"),
    ("analyze this sales dataset into a report", "data_report"),
    ("I need a quarterly business review", "periodic_review"),
    ("make a one-pager for leadership", "executive_brief"),
])
def test_classify_profile_keywords(intent, expected):
    assert classify_profile(intent).name == expected


def test_explicit_profile_wins_over_keywords():
    # "executive" keyword would normally pick executive_brief, but an explicit
    # pitch_narrative must win.
    assert classify_profile(
        "executive summary", explicit="pitch_narrative"
    ).name == "pitch_narrative"


def test_unknown_intent_defaults_to_data_report():
    assert classify_profile("tell me a joke").name == "data_report"
