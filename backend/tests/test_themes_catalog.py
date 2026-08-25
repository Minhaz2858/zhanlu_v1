"""Tests for the HTML design theme catalog."""
from app.services.artifacts.themes import THEME_CATALOG, get_theme, ThemePreset


class TestThemeCatalog:
    def test_catalog_has_12_presets(self):
        assert len(THEME_CATALOG) == 12

    def test_all_preset_names_are_lowercase_underscore(self):
        for name in THEME_CATALOG:
            assert name == name.lower(), f"{name!r} should be lowercase"
            assert " " not in name, f"{name!r} should use underscores"

    def test_all_presets_have_required_tokens(self):
        required = {"bg_primary", "text_primary", "accent"}
        for name, preset in THEME_CATALOG.items():
            missing = required - set(preset.color_tokens)
            assert not missing, f"{name} missing tokens: {missing}"

    def test_all_presets_have_fonts(self):
        for name, preset in THEME_CATALOG.items():
            assert preset.font_display, f"{name} missing font_display"
            assert preset.font_body, f"{name} missing font_body"

    def test_get_theme_returns_known(self):
        preset = get_theme("bold_signal")
        assert preset.display_name

    def test_get_theme_raises_on_unknown(self):
        import pytest
        with pytest.raises(KeyError):
            get_theme("nope")


class TestKnownThemes:
    def test_bold_signal_colors(self):
        preset = get_theme("bold_signal")
        assert preset.color_tokens["bg_primary"] == "#1a1a1a"
        assert preset.color_tokens["accent"] == "#FF5722"

    def test_electric_studio_colors(self):
        preset = get_theme("electric_studio")
        assert preset.color_tokens["accent"] == "#4361ee"

    def test_vintage_editorial_has_warm_bg(self):
        preset = get_theme("vintage_editorial")
        assert preset.color_tokens["bg_primary"].startswith("#f") or \
               preset.color_tokens["bg_primary"].startswith("#e")
