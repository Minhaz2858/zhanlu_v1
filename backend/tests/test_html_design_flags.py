"""Tests for the HTML design renderer config flags (all default OFF)."""
from app.config import settings


class TestHtmlDesignFlags:
    def test_html_design_renderer_disabled_by_default(self):
        assert settings.HTML_DESIGN_RENDERER_ENABLED is False

    def test_html_design_editable_disabled_by_default(self):
        assert settings.HTML_DESIGN_EDITABLE_ENABLED is False

    def test_html_design_themes_empty_by_default(self):
        assert settings.HTML_DESIGN_THEMES == []
