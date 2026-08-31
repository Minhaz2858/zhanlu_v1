"""Tests for pick_pptx_mode — v1.1 editable_text architecture."""
from app.services.artifacts.deck_router import pick_pptx_mode
from app.services.synexia.contracts import DeckPlan, SlidePlan


def _plan():
    return DeckPlan(title="T", deck_type="data_report", slides=[SlidePlan(layout="cover", title="C")])


class TestPickPptxMode:
    def test_default_is_image_fill_when_disabled(self, monkeypatch):
        # HTML_DESIGN_EDITABLE_ENABLED defaults False in code (on in .env).
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", False)
        assert pick_pptx_mode(_plan(), "make a beautiful deck") == "image_fill"

    def test_default_is_editable_text_when_enabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        # Editable-native is the DEFAULT — no keyword required (2026-08-29:
        # users expect a downloaded deck to be editable in PowerPoint).
        assert pick_pptx_mode(_plan(), "make a beautiful market ppt") == "editable_text"

    def test_static_request_returns_image_fill_when_enabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        assert pick_pptx_mode(_plan(), "keep it as a static picture deck") == "image_fill"
        assert pick_pptx_mode(_plan(), "图片形式") == "image_fill"

    def test_edit_keyword_returns_editable_text_when_enabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        assert pick_pptx_mode(_plan(), "let me edit the slides") == "editable_text"

    def test_edit_keyword_returns_image_fill_when_disabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", False)
        assert pick_pptx_mode(_plan(), "let me edit the slides") == "image_fill"

    def test_tweak_keyword(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        assert pick_pptx_mode(_plan(), "tweak the bullets") == "editable_text"
