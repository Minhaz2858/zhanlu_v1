"""Tests for pick_pptx_mode — v1.1 editable_text architecture."""
from app.services.artifacts.deck_router import pick_pptx_mode
from app.services.synexia.contracts import DeckPlan, SlidePlan


def _plan():
    return DeckPlan(title="T", deck_type="data_report", slides=[SlidePlan(layout="cover", title="C")])


class TestPickPptxMode:
    def test_default_is_image_fill(self):
        assert pick_pptx_mode(_plan(), "make a beautiful deck") == "image_fill"

    def test_edit_keyword_returns_editable_text_when_enabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        assert pick_pptx_mode(_plan(), "let me edit the slides") == "editable_text"

    def test_edit_keyword_returns_image_fill_when_disabled(self):
        # default False
        assert pick_pptx_mode(_plan(), "let me edit the slides") == "image_fill"

    def test_tweak_keyword(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        assert pick_pptx_mode(_plan(), "tweak the bullets") == "editable_text"
