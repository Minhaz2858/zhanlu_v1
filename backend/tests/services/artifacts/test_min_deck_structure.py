"""Tests for the minimum-deck structure guarantee (_enrich_thin_plan).

Regression: a summary-only payload (the chat-path shape when the agent
did not collect rows) used to collapse to a 2-slide "Data Overview /
Thank you" deck.  _enrich_thin_plan must pad any thin plan to >= 3
content slides so the user always receives a real deliverable.
"""

import pytest

from app.services.artifacts.exporters.service import (
    _NON_CONTENT_LAYOUTS,
    _deck_has_enough_content,
    _enrich_thin_plan,
)
from app.services.synexia.contracts import (
    DeckPlan,
    InsightSpec,
    KPISpec,
    ReportCardPayload,
    SlidePlan,
)


def _content_count(plan) -> int:
    return sum(
        1 for s in plan.slides if getattr(s, "layout", "") not in _NON_CONTENT_LAYOUTS
    )


def test_skeleton_plan_is_padded_to_three_content_slides() -> None:
    """cover + closing (2 slides) -> >= 3 content slides after enrichment."""
    plan = DeckPlan(
        title="Data Overview",
        slides=[
            SlidePlan(layout="cover", title="Data Overview"),
            SlidePlan(layout="closing", title="Thank you"),
        ],
    )
    payload = ReportCardPayload(title="Data Overview")
    _enrich_thin_plan(plan, payload)

    assert _content_count(plan) >= 3, plan.slides
    # Every padding slide must be a real content layout.
    for s in plan.slides:
        assert s.layout in (
            "cover", "closing", "kpi_grid", "findings_cards",
            "insights_bullets", "recommendations",
        )


def test_summary_payload_becomes_exec_summary_slide() -> None:
    """A summary-only payload gets a real 'Executive Summary' slide."""
    plan = DeckPlan(
        title="C5/C9 Market View",
        slides=[
            SlidePlan(layout="cover", title="C5/C9 Market View"),
            SlidePlan(layout="closing", title="Thank you"),
        ],
    )
    payload = ReportCardPayload(
        title="C5/C9 Market View",
        summary="C5/C9 prices firmed in Q3 on tight supply. "
                "Demand from downstream polyolefin converters stayed resilient. "
                "Margins compressed as feedstock costs rose.",
    )
    _enrich_thin_plan(plan, payload)

    layouts = [s.layout for s in plan.slides]
    assert "insights_bullets" in layouts
    exec_summary = next(s for s in plan.slides if s.layout == "insights_bullets")
    assert exec_summary.title == "Executive Summary"
    assert len(exec_summary.bullets or []) >= 1


def test_kpis_and_findings_are_used_for_padding() -> None:
    """KPIs + findings in the payload become real kpi_grid / findings slides."""
    plan = DeckPlan(
        title="Q2 Review",
        slides=[
            SlidePlan(layout="cover", title="Q2 Review"),
            SlidePlan(layout="closing", title="Thank you"),
        ],
    )
    payload = ReportCardPayload(
        title="Q2 Review",
        kpis=[KPISpec(label="Revenue", value="¥1.2B", delta="+8%")],
        key_findings=[InsightSpec(text="Revenue grew 8% QoQ on volume")],
    )
    _enrich_thin_plan(plan, payload)

    layouts = [s.layout for s in plan.slides]
    assert "kpi_grid" in layouts
    assert "findings_cards" in layouts
    assert _content_count(plan) >= 3


def test_already_substantive_plan_is_untouched() -> None:
    """A plan with >= 3 content slides is NOT modified."""
    plan = DeckPlan(
        title="Full Deck",
        slides=[
            SlidePlan(layout="cover", title="Full Deck"),
            SlidePlan(layout="kpi_grid", title="Key Metrics", kpi_specs=[]),
            SlidePlan(layout="chart_full", title="Trend"),
            SlidePlan(layout="findings_cards", title="Findings", bullets=["x"]),
            SlidePlan(layout="closing", title="Thank you"),
        ],
    )
    before = [s.layout for s in plan.slides]
    _enrich_thin_plan(plan, ReportCardPayload(title="Full Deck"))
    assert [s.layout for s in plan.slides] == before


def test_deck_has_enough_content_floor() -> None:
    """_deck_has_enough_content accepts >= 3 content slides, rejects less."""
    thin = DeckPlan(title="t", slides=[
        SlidePlan(layout="cover", title="t"),
        SlidePlan(layout="insights_bullets", title="Summary", bullets=["a"]),
        SlidePlan(layout="closing", title="Thanks"),
    ])
    assert _deck_has_enough_content(thin) is False

    ok = DeckPlan(title="t", slides=[
        SlidePlan(layout="cover", title="t"),
        SlidePlan(layout="kpi_grid", title="K", kpi_specs=[]),
        SlidePlan(layout="chart_full", title="C"),
        SlidePlan(layout="findings_cards", title="F", bullets=["x"]),
        SlidePlan(layout="closing", title="Thanks"),
    ])
    assert _deck_has_enough_content(ok) is True
    assert _deck_has_enough_content(None) is False
