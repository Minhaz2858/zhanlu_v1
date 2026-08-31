"""Tests for select_theme() — keyword + deck_type resolution."""
from app.services.artifacts.themes import select_theme, THEME_CATALOG
from app.services.synexia.contracts import DeckPlan, SlidePlan


def _catalog_names() -> set[str]:
    return {getattr(p, "name", "") for p in THEME_CATALOG.values()} - {""}


def _plan(deck_type: str = "data_report") -> DeckPlan:
    return DeckPlan(title="T", deck_type=deck_type, slides=[SlidePlan(layout="cover", title="C")])


class TestSelectTheme:
    def test_investor_deck_default(self):
        assert select_theme(_plan("investor_deck")).name == "bold_signal"

    def test_marketing_default(self):
        assert select_theme(_plan("marketing")).name == "creative_voltage"

    def test_executive_brief_default(self):
        assert select_theme(_plan("executive_brief")).name == "paper_and_ink"

    def test_training_default(self):
        assert select_theme(_plan("training")).name == "notebook_tabs"

    def test_data_report_default(self):
        assert select_theme(_plan("data_report")).name == "electric_studio"

    def test_keyword_wellness_overrides(self):
        assert select_theme(_plan("marketing"), "wellness program").name == "dark_botanical"

    def test_keyword_editorial_overrides(self):
        assert select_theme(_plan("data_report"), "editorial report").name == "vintage_editorial"

    def test_keyword_tech_overrides(self):
        assert select_theme(_plan("executive_brief"), "tech update").name == "neon_cyber"

    def test_unknown_deck_type_hash_rotates(self):
        # No style word / planner pick / content word / deck-type default →
        # hash rotation over the catalog (deterministic per message), NOT a
        # fixed fallback. Same message → same theme (2026-08-29 theme variety).
        assert select_theme(_plan("nonexistent_type")).name == select_theme(_plan("nonexistent_type")).name
        assert select_theme(_plan("nonexistent_type")).name in _catalog_names()

    def test_no_plan_hash_rotates(self):
        assert select_theme(None).name in _catalog_names()
        # Deterministic: identical (no-signal) input → identical theme.
        assert select_theme(None).name == select_theme(None).name

    def test_case_insensitive_keyword(self):
        assert select_theme(_plan("data_report"), "WELLNESS recap").name == "dark_botanical"

    def test_settings_filter_narrows_catalog(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_THEMES", ["bold_signal"])
        # "editorial" keyword + "data_report" default are both filtered out
        # → fall through to the first available preset (bold_signal).
        result = select_theme(_plan("data_report"), "editorial report")
        assert result.name == "bold_signal"
