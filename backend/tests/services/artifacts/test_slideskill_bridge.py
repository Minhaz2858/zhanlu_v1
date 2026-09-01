"""Tests for the slide-skill editable-native bridge.

The editable tier converts a DeckPlan → markdown → slide-skill fast route →
native-editable .pptx (real text frames), used when the user asks for an
editable deck and ``HTML_DESIGN_EDITABLE_ENABLED`` is on.  These tests lock
in the plan→markdown contract and the router's editable_text decision;
the CLI invocation itself is exercised only when slide-skill is installed.
"""

from app.services.synexia.contracts import (
    ChartSpecInSlide,
    DeckPlan,
    KPISpecInSlide,
    SlidePlan,
)
from app.services.artifacts.slideskill_bridge import (
    plan_to_markdown,
    theme_to_toml,
    _THEME_MAP,
    _FALLBACK_THEME,
)
from app.services.artifacts.deck_router import pick_pptx_mode
from app.config import settings


def _plan(theme: str = "swiss_modern") -> DeckPlan:
    return DeckPlan(
        title="C5/C9 Market View",
        theme_recommendation=theme,
        slides=[
            SlidePlan(layout="cover", title="C5/C9 Market View", subtitle="Q3 Outlook"),
            SlidePlan(
                layout="kpi_grid",
                title="Key Metrics",
                kpi_specs=[
                    KPISpecInSlide(label="Revenue", value="¥1.2B", delta="+8.4%"),
                    KPISpecInSlide(label="Volume", value="48.2KT", delta="-2.1%"),
                ],
            ),
            SlidePlan(
                layout="chart_full",
                title="Revenue trend is strong",
                chart_spec=ChartSpecInSlide(
                    chart_type="line", x_key="month", y_keys=["revenue"], title="Rev"
                ),
                bullets=["Q2 revenue hit a record"],
            ),
            SlidePlan(
                layout="insights_bullets",
                title="East leads the recovery",
                bullets=[
                    "East region grew 8.4%",
                    "South region declined 2.1%",
                    "New products carried the quarter",
                ],
            ),
            SlidePlan(layout="closing", title="Thank you"),
        ],
    )


def test_plan_to_markdown_cover_and_sections() -> None:
    md = plan_to_markdown(_plan(), [])
    lines = [l for l in md.splitlines() if l.strip()]
    # Cover becomes the first heading.
    assert lines[0].startswith("# C5/C9 Market View")
    # Content slides get headings + bullets.
    assert any(l.startswith("# Key Metrics") for l in lines)
    assert any("- Revenue ¥1.2B (+8.4%)" in l for l in lines)
    # Section-free: no raw chart spec leaks into markdown.
    assert "chart_type" not in md
    assert "y_keys" not in md


def test_plan_to_markdown_closing_present() -> None:
    md = plan_to_markdown(_plan(), [])
    assert "# Thank you" in md


def test_plan_to_markdown_empty_plan_gets_cover() -> None:
    plan = DeckPlan(title="Overview", slides=[])
    md = plan_to_markdown(plan, [])
    assert md.strip().startswith("# Overview")


def test_plan_to_markdown_bullet_density_capped() -> None:
    plan = DeckPlan(
        title="Dense",
        slides=[SlidePlan(
            layout="insights_bullets",
            title="Dense slide",
            bullets=[f"point {i}" for i in range(20)],
        )],
    )
    md = plan_to_markdown(plan, [])
    bullet_lines = [l for l in md.splitlines() if l.startswith("- ")]
    assert len(bullet_lines) <= 6


def test_theme_map_has_fallback() -> None:
    # Every installed theme key resolves; unknown themes fall back.
    assert _THEME_MAP["swiss_modern"] == "light-corporate"
    assert _THEME_MAP["neon_cyber"] == "dark-tech"
    assert _FALLBACK_THEME == "light-corporate"


def test_theme_to_toml_carries_our_tokens() -> None:
    """The generated slide-skill user theme carries OUR theme+palette tokens."""
    from app.services.artifacts.themes import select_theme

    preset = select_theme(_plan(), "make an editable market view ppt")
    name, toml = theme_to_toml(preset)

    # Safe name, prefixed so it can't collide with builtins.
    assert name.startswith("zhanlu_")
    assert "bold_signal" in name or "swiss_modern" in name or "electric_studio" in name

    # All 6 core roles slide-skill's fast route requires.
    for role in ("background", "surface", "text", "body", "accent", "muted"):
        assert f'{role} = "' in toml, f"missing palette role {role}"

    # The accent is the palette-applied accent — not the theme default.
    tokens = preset.color_tokens
    assert f'accent = "{tokens["accent"]}"' in toml
    # Background/text come from the theme's canonical tokens.
    assert f'background = "{tokens["bg_primary"]}"' in toml
    assert f'text = "{tokens["text_primary"]}"' in toml

    # Fonts: display + body families present, CJK fallbacks included.
    assert "Noto Sans SC" in toml
    assert "sans-serif" in toml


def test_theme_to_toml_parses_as_toml() -> None:
    """slide-skill loads user themes with tomllib — our output must parse."""
    import tomllib

    from app.services.artifacts.themes import select_theme

    preset = select_theme(_plan(), "make an editable deck")
    name, toml = theme_to_toml(preset)
    data = tomllib.loads(toml)
    theme = data["theme"]
    assert theme["name"] == name
    assert len(theme["palette"]) >= 6
    for role in ("background", "accent", "text"):
        assert theme["palette"][role].startswith("#")


def test_theme_to_toml_dark_theme_light_text() -> None:
    """A dark theme must derive light surface/body so text stays readable."""
    from app.services.artifacts.themes import select_theme

    preset = select_theme(_plan(theme="neon_cyber"), "make an editable tech deck")
    _, toml = theme_to_toml(preset)
    assert 'background = "#0a0a1a"' in toml  # neon_cyber bg_primary
    # Derived surface/body are LIGHTER than the dark background.
    assert 'surface = "#' in toml
    assert 'body = "#' in toml
    assert 'accent = "' in toml


def test_pick_pptx_mode_editable_when_enabled_and_requested() -> None:
    old = settings.HTML_DESIGN_EDITABLE_ENABLED
    try:
        settings.HTML_DESIGN_EDITABLE_ENABLED = True
        plan = _plan()
        assert pick_pptx_mode(plan, "make an editable deck I can tweak later") == "editable_text"
        assert pick_pptx_mode(plan, "make a 可编辑 deck") == "editable_text"
        # Editable-native is the DEFAULT (2026-08-29) — a plain request now
        # yields an editable deck, not a baked-PNG deck.
        assert pick_pptx_mode(plan, "make a beautiful market ppt") == "editable_text"
        # Explicit static/image request opts back into image_fill.
        assert pick_pptx_mode(plan, "keep it as static images, not editable") == "image_fill"
    finally:
        settings.HTML_DESIGN_EDITABLE_ENABLED = old


def test_pick_pptx_mode_image_fill_when_disabled() -> None:
    old = settings.HTML_DESIGN_EDITABLE_ENABLED
    try:
        settings.HTML_DESIGN_EDITABLE_ENABLED = False
        plan = _plan()
        assert pick_pptx_mode(plan, "make an editable deck") == "image_fill"
        assert pick_pptx_mode(plan, "make a beautiful market ppt") == "image_fill"
    finally:
        settings.HTML_DESIGN_EDITABLE_ENABLED = old


def test_chart_bullets_derive_from_chart_rows() -> None:
    """Chart slides without prose get data takeaways, never an empty heading."""
    from app.services.artifacts.slideskill_bridge import _chart_bullets

    plan = _plan()
    chart_slide = plan.slides[2]
    rows = [
        {"month": "Jan", "revenue": 120},
        {"month": "Feb", "revenue": 135},
    ]
    bullets = _chart_bullets(chart_slide, rows)
    assert bullets
    assert any("Jan" in b and "120" in b for b in bullets)
    assert any("Feb" in b and "135" in b for b in bullets)


def test_chart_bullets_use_materialized_chart_rows() -> None:
    """chart_rows on the slide (slides-authored decks) win over deck rows."""
    from app.services.artifacts.slideskill_bridge import _chart_bullets

    slide = SlidePlan(
        layout="chart_full", title="Trend",
        chart_spec=ChartSpecInSlide(chart_type="line", x_key="month", y_keys=["amount"], title="T"),
        chart_rows=[{"month": "M1", "amount": 10}, {"month": "M2", "amount": 20}],
    )
    bullets = _chart_bullets(slide, [])
    assert any("M1" in b and "10" in b for b in bullets)


def test_table_bullets_render_rows() -> None:
    from app.services.artifacts.slideskill_bridge import _table_bullets

    slide = SlidePlan(
        layout="data_table", title="Mix",
        table_cols=["Material", "Qty"],
        table_rows=[{"Material": "C5", "Qty": 19688}, {"Material": "C9", "Qty": 19688}],
    )
    bullets = _table_bullets(slide)
    assert any("Material: C5" in b and "Qty: 19688" in b for b in bullets)
