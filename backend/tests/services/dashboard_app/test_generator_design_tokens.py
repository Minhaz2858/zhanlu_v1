"""Tests for the generator's design-token loading + normalization.

Covers:
- reading the design-system.json sidecar via design_system_ref
- normalization into the frontend token shape (colors/typography/spacing/style)
- chart palette derivation (contrast-safe, capped at 6)
- dark-mode overrides
- graceful fallback when no design system is attached
- structured-payload shortcut (builtin fallback path)
"""

import json

import pytest

from app.services.dashboard_app.generator import DashboardAppGenerator


@pytest.fixture
def gen():
    return DashboardAppGenerator()


@pytest.fixture
def sample_design() -> dict:
    return {
        "project_name": "ERP Sales",
        "category": "Analytics Dashboard",
        "pattern": {"name": "Enterprise Gateway", "sections": "KPI > Charts", "cta_placement": "Top"},
        "style": {"name": "Data-Dense Dashboard", "keywords": "BI, dense", "best_for": "analytics"},
        "colors": {
            "primary": "#0F172A",
            "on_primary": "#FFFFFF",
            "secondary": "#1E293B",
            "accent": "#22C55E",
            "background": "#020617",
            "foreground": "#F8FAFC",
            "muted": "#1A1E2F",
            "border": "#334155",
            "destructive": "#EF4444",
            "ring": "#22C55E",
        },
        "typography": {
            "heading": "Fira Code",
            "body": "Fira Sans",
            "google_fonts_url": "https://fonts.googleapis.com/css2?family=Fira",
            "css_import": "@import url('...');",
        },
        "spacing_scale": {"xs": "2px", "sm": "4px", "md": "8px", "lg": "12px", "xl": "16px", "2xl": "24px", "3xl": "32px"},
    }


def test_normalize_basic_structure(gen, sample_design):
    tokens = gen._normalize_design(sample_design)
    assert set(tokens) == {"colors", "typography", "spacing", "style"}
    assert tokens["colors"]["primary"] == "#0F172A"
    assert tokens["colors"]["background"] == "#020617"
    assert tokens["typography"]["heading"] == "Fira Code"
    assert tokens["spacing"]["md"] == "8px"
    assert tokens["style"]["name"] == "Data-Dense Dashboard"


def test_normalize_chart_palette_contrast_safe(gen, sample_design):
    tokens = gen._normalize_design(sample_design)
    palette = tokens["colors"]["chart_palette"]
    assert len(palette) == 6

    def lum(h):
        h = h.lstrip("#")
        return 0.299 * int(h[0:2], 16) + 0.587 * int(h[2:4], 16) + 0.114 * int(h[4:6], 16)

    bg = lum(tokens["colors"]["background"])
    for color in palette:
        assert abs(lum(color) - bg) > 60, f"{color} not contrast-safe against background"


def test_normalize_dark_mode_overrides(gen, sample_design):
    # Dark-native design: dark background should be reused as the dark override.
    tokens = gen._normalize_design(sample_design)
    assert tokens["colors"]["dark"]["background"] == "#020617"

    # Light design: dark overrides come from the explicit dark block or defaults.
    light = dict(sample_design)
    light["colors"] = dict(sample_design["colors"])
    light["colors"]["background"] = "#F8FAFC"
    light["colors"]["foreground"] = "#0F172A"
    tokens = gen._normalize_design(light)
    assert tokens["colors"]["dark"]["background"] == "#020617"


def test_load_design_tokens_absent_ref_returns_empty(gen):
    spec = {"slug": "x", "metrics": [], "datasource_id": "d"}
    assert gen._load_design_tokens(spec) == {}


def test_load_design_tokens_from_structured_payload(gen, sample_design):
    spec = {"slug": "x", "metrics": [], "datasource_id": "d", "structured": sample_design}
    tokens = gen._load_design_tokens(spec)
    assert tokens["colors"]["primary"] == "#0F172A"


def test_load_design_tokens_from_sidecar(tmp_path, gen, sample_design):
    """design_system_ref -> sibling design-system.json sidecar is read."""
    from app.config import settings

    original = settings.GENERATED_DIR
    settings.GENERATED_DIR = str(tmp_path / "generated")
    try:
        out_dir = tmp_path / "generated" / "design-system" / "org-1"
        out_dir.mkdir(parents=True)
        (out_dir / "MASTER.md").write_text("# dummy", encoding="utf-8")
        (out_dir / "design-system.json").write_text(
            json.dumps(sample_design), encoding="utf-8"
        )

        spec = {
            "slug": "x",
            "metrics": [],
            "datasource_id": "d",
            "design_system_ref": "design-system/org-1/MASTER.md",
        }
        tokens = gen._load_design_tokens(spec)
        assert tokens["colors"]["primary"] == "#0F172A"
        assert tokens["typography"]["heading"] == "Fira Code"
    finally:
        settings.GENERATED_DIR = original


def test_missing_sidecar_falls_back_to_empty(tmp_path, gen):
    from app.config import settings

    original = settings.GENERATED_DIR
    settings.GENERATED_DIR = str(tmp_path / "generated")
    try:
        spec = {
            "slug": "x",
            "metrics": [],
            "datasource_id": "d",
            "design_system_ref": "design-system/org-1/MASTER.md",
        }
        assert gen._load_design_tokens(spec) == {}
    finally:
        settings.GENERATED_DIR = original


def test_frontend_config_includes_design_field(gen, sample_design):
    spec = {
        "slug": "x",
        "name": "X",
        "datasource_id": "d",
        "design_system_ref": None,
        "structured": sample_design,
        "theme": "dark",
        "metrics": [{"id": "m1", "title": "M1", "type": "kpi", "options": {}}],
    }
    config = gen._frontend_config(spec)
    assert "design" in config
    assert config["design"]["colors"]["primary"] == "#0F172A"
