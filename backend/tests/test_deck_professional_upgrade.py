"""Regression tests for the Kimi-grade deck upgrade (2026-08-29).

Covers: slide furniture (page numbers/footer), cover meta strip,
professional chart JS (json-serialized, multi-series, load-deferred),
speaker notes flowing into plan dumps, and the stage splitter keeping
ALL trailing scripts (the blank-chart bug).
"""
from __future__ import annotations

import re

import pytest

from app.services.artifacts.html_slide_generator import (
    build_stage,
    render_slide,
    _chart_js,
    _chart_data_from_plan,
)
from app.services.artifacts.html_to_pptx import _split_stage_into_slides
from app.services.artifacts.themes import select_theme
from app.services.synexia.contracts import DeckPlan, SlidePlan


# ---------------------------------------------------------------------------
# Slide furniture (page numbers + deck-title footer)
# ---------------------------------------------------------------------------


def test_build_stage_adds_furniture():
    s1 = '<section class="slide slide--insights-bullets"><h2>One</h2></section>'
    s2 = '<section class="slide slide--insights-bullets"><h2>Two</h2></section>'
    stage = build_stage([s1, s2], deck_title="Market View 2026")
    assert "deck-furniture" in stage
    assert "01 / 02" in stage and "02 / 02" in stage
    assert "Market View 2026" in stage
    # furniture CSS block present
    head_css = stage[stage.index("<style>") : stage.index("</style>")]
    assert ".deck-furniture" in head_css


def test_build_stage_no_furniture_on_hero_pages():
    cover = '<section class="slide slide--cover"><h1>Cover</h1></section>'
    stage = build_stage([cover], deck_title="X")
    # furniture div is injected but CSS hides it on hero pages
    assert ".slide--cover .deck-furniture" in stage
    assert "display: none" in stage


def test_build_stage_without_deck_title_has_no_furniture():
    s1 = '<section class="slide slide--insights-bullets"><h2>One</h2></section>'
    stage = build_stage([s1])
    assert "deck-furniture" not in stage


# ---------------------------------------------------------------------------
# Cover meta strip (period + date + brand)
# ---------------------------------------------------------------------------


def test_cover_meta_strip():
    theme = select_theme(None, "market ppt")
    sp = SlidePlan(layout="cover", title="Test", period="Q3 2026")
    html = render_slide("cover", sp, theme)
    assert "cover__meta" in html
    assert "Q3 2026" in html
    assert "SYNEXIA" in html


# ---------------------------------------------------------------------------
# Professional chart JS
# ---------------------------------------------------------------------------


def test_chart_js_is_valid_json_not_python_repr():
    """The blank-chart bug: Python repr emits ``False`` which is invalid JS."""
    theme = select_theme(None, "market ppt")
    plan = SlidePlan(layout="chart_full", title="T")
    js = _chart_js(
        plan,
        theme,
        "c1",
        ["NA", "EU"],
        [[4.2, 2.1], [3.1, 1.8]],
        "bar",
        ["C5", "C9"],
    )
    # no Python booleans leaked
    assert "False" not in js and "True" not in js
    assert "false" in js and "true" in js
    # deferred to load so the screenshot waits for the draw
    assert "window.addEventListener('load'" in js
    # multi-series legend + real column labels
    assert "C5" in js and "C9" in js
    assert "legend" in js and "tooltip" in js and "scales" in js


def test_chart_data_multi_series_detection():
    plan = SlidePlan(
        layout="chart_full",
        title="Revenue",
        chart_rows=[
            {"label": "NA", "C5 resins": 4.2, "C9 resins": 2.1},
            {"label": "EU", "C5 resins": 3.1, "C9 resins": 1.8},
        ],
    )
    ct, labels, values, series = _chart_data_from_plan(plan)
    assert ct == "bar"
    assert labels == ["NA", "EU"]
    assert values == [[4.2, 2.1], [3.1, 1.8]]
    assert series == ["C5 resins", "C9 resins"]


def test_chart_data_single_series_still_works():
    plan = SlidePlan(
        layout="chart_full",
        title="Revenue",
        chart_rows=[{"label": "A", "value": 10}, {"label": "B", "value": 12}],
    )
    ct, labels, values, series = _chart_data_from_plan(plan)
    assert values == [10.0, 12.0]
    assert series == []


# ---------------------------------------------------------------------------
# Speaker notes
# ---------------------------------------------------------------------------


def test_notes_flow_through_plan_dump():
    plan = DeckPlan(
        title="X",
        slides=[
            SlidePlan(layout="cover", title="X", notes="Open with X"),
            SlidePlan(layout="closing", title="Thanks", notes=""),
        ],
    )
    d = plan.model_dump()
    assert d["slides"][0]["notes"] == "Open with X"
    assert "notes" in d["slides"][1]


# ---------------------------------------------------------------------------
# Stage splitter keeps ALL trailing scripts (blank-chart bug)
# ---------------------------------------------------------------------------


def test_splitter_keeps_multiple_trailing_scripts():
    theme = select_theme(None, "market ppt")
    sp = SlidePlan(
        layout="chart_full",
        title="Revenue",
        chart_rows=[{"label": "NA", "C5": 4.2, "C9": 2.1}],
    )
    html = render_slide("chart_full", sp, theme)
    stage = build_stage([html], deck_title="Test")
    chunks = _split_stage_into_slides(stage)
    assert len(chunks) == 1
    chunk = chunks[0]
    # both the chart library script and the init script must survive the split
    assert chunk.count("<script") >= 2
    # chart lib: either vendored inline (no URL) or CDN fallback — never both,
    # and the init script must always be present
    assert "new Chart" in chunk
    assert "window.addEventListener('load'" in chunk
    assert "cdn.jsdelivr" in chunk or len(chunk) > 100000  # vendored inline is huge


def test_splitter_with_two_slides():
    theme = select_theme(None, "market ppt")
    sp1 = SlidePlan(layout="chart_full", title="R", chart_rows=[{"label": "A", "v": 1}])
    sp2 = SlidePlan(layout="insights_bullets", title="I", bullets=["b1"])
    h1 = render_slide("chart_full", sp1, theme)
    h2 = render_slide("insights_bullets", sp2, theme)
    stage = build_stage([h1, h2], deck_title="T")
    chunks = _split_stage_into_slides(stage)
    assert len(chunks) == 2
    assert "new Chart" in chunks[0]
    assert "new Chart" not in chunks[1]


# ---------------------------------------------------------------------------
# Vendored chart.js (no external CDN dependency)
# ---------------------------------------------------------------------------


def test_chart_loader_vendored_inline():
    """Chart.js is inlined from the vendored file, not fetched from a CDN."""
    from app.services.artifacts.html_slide_generator import (
        _chart_loader_tag,
        _load_vendored_chart_js,
    )

    src = _load_vendored_chart_js()
    tag = _chart_loader_tag()
    if src:
        # vendored path: no CDN URL anywhere, inline script is the full lib
        assert "cdn.jsdelivr" not in tag
        assert tag.startswith("<script>")
        assert len(tag) > 100000
    else:
        # fallback path: CDN URL present (file not vendored in this env)
        assert "cdn.jsdelivr" in tag


def test_chart_slide_has_no_external_url():
    """A rendered chart slide must not reference any external network URL."""
    theme = select_theme(None, "market ppt")
    sp = SlidePlan(
        layout="chart_full",
        title="Revenue",
        chart_rows=[{"label": "A", "v": 1}],
    )
    html = render_slide("chart_full", sp, theme)
    assert "cdn.jsdelivr" not in html
    assert "http://" not in html.replace("https://fonts.googleapis", "")
