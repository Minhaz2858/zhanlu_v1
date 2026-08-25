"""Tests for the HTML stage builder (wraps slides into index.html)."""
from app.services.artifacts.html_slide_generator import build_stage, render_slide
from app.services.artifacts.themes import get_theme
from app.services.synexia.contracts import SlidePlan


THEME = get_theme("bold_signal")


def test_stage_wraps_in_html():
    htmls = [render_slide("cover", SlidePlan(layout="cover", title="T"), THEME)]
    stage = build_stage(htmls)
    assert stage.startswith("<!DOCTYPE html>")
    assert "</html>" in stage


def test_stage_includes_all_slides():
    htmls = [
        render_slide("cover", SlidePlan(layout="cover", title="One"), THEME),
        render_slide("cover", SlidePlan(layout="cover", title="Two"), THEME),
    ]
    stage = build_stage(htmls)
    assert "One" in stage
    assert "Two" in stage


def test_stage_uses_16_9_dimensions():
    stage = build_stage([render_slide("cover", SlidePlan(layout="cover", title="T"), THEME)])
    assert "1920px" in stage
    assert "1080px" in stage


def test_stage_breaks_pages_between_slides():
    stage = build_stage([
        render_slide("cover", SlidePlan(layout="cover", title="One"), THEME),
        render_slide("cover", SlidePlan(layout="cover", title="Two"), THEME),
    ])
    assert "page-break-after" in stage


def test_stage_escapes_user_content():
    stage = build_stage([
        render_slide("cover", SlidePlan(layout="cover", title="<script>"), THEME),
    ])
    assert "&lt;script&gt;" in stage
