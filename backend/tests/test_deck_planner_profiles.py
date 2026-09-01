"""Phase 4 — profile-aware fallback deck planning.

Verifies that ``_fallback_plan`` (and therefore ``build_deck_plan`` when the
LLM planner is disabled) produces a *different, profile-appropriate* slide
structure for each of the four profiles.
"""

from __future__ import annotations

from app.services.artifacts.deck_planner import _fallback_plan
from app.services.artifacts.deck_profiles import get_profile
from app.services.artifacts.data_profiler import profile_rows


def _sample_rows():
    return [
        {"month": "Jan", "revenue": 120, "cost": 80},
        {"month": "Feb", "revenue": 135, "cost": 90},
        {"month": "Mar", "revenue": 150, "cost": 95},
        {"month": "Apr", "revenue": 142, "cost": 88},
        {"month": "May", "revenue": 160, "cost": 100},
    ]


def _profile():
    return profile_rows(_sample_rows())


def _layouts(plan):
    return [s.layout for s in plan.slides]


def test_data_report_fallback_structure():
    plan = _fallback_plan("quarterly revenue report", _profile(), _sample_rows(),
                          profile_name="data_report")
    layouts = _layouts(plan)
    assert layouts[0] == "cover"
    assert "kpi_grid" in layouts
    assert "chart_full" in layouts
    # data_report keeps raw tables for big datasets and never forbids them.
    assert plan.deck_type == "data_report"


def test_executive_brief_has_no_tables_and_is_short():
    prof = get_profile("executive_brief")
    plan = _fallback_plan("executive summary for the board", _profile(), _sample_rows(),
                          profile_name="executive_brief")
    layouts = _layouts(plan)
    assert "data_table" not in layouts, "executive_brief must not include raw tables"
    assert prof.forbid_tables is True
    # Enforced ceiling.
    assert len(plan.slides) <= prof.slide_count_range[1]
    assert "chart_with_bullets" in layouts
    assert plan.deck_type == "executive_brief"


def test_pitch_narrative_has_story_arc():
    plan = _fallback_plan("pitch deck to raise our seed round", _profile(), _sample_rows(),
                          profile_name="pitch_narrative")
    layouts = _layouts(plan)
    assert "section_divider" in layouts
    assert "findings_cards" in layouts
    assert "recommendations" in layouts
    assert layouts[-1] == "closing"
    assert plan.deck_type == "pitch_narrative"


def test_periodic_review_has_trend_and_insights():
    plan = _fallback_plan("weekly shipment review", _profile(), _sample_rows(),
                          profile_name="periodic_review")
    layouts = _layouts(plan)
    assert "kpi_grid" in layouts
    assert "chart_full" in layouts
    assert "insights_bullets" in layouts
    assert plan.deck_type == "periodic_review"


def test_explicit_profile_name_respected_in_build():
    import asyncio

    from app.config import settings
    from app.services.artifacts.deck_planner import build_deck_plan

    # Force the deterministic fallback path.
    prev = settings.PPT_DECK_PLANNER_ENABLED
    settings.PPT_DECK_PLANNER_ENABLED = False
    try:
        plan, _ = asyncio.run(
            build_deck_plan(
                "executive summary for the board", _sample_rows(),
                profile_name="executive_brief",
            )
        )
    finally:
        settings.PPT_DECK_PLANNER_ENABLED = prev
    assert plan.deck_type == "executive_brief"
    assert "data_table" not in [s.layout for s in plan.slides]
