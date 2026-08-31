"""Theme + palette variety regression tests.

The "every deck looks the same" complaint traced back to theme selection:
content keywords (market/industry/report) fired BEFORE the planner's
theme_recommendation, forcing every business deck into swiss_modern — and
the planner schema only allowed two theme names that don't exist in the
12-theme catalog.  These tests lock in the fix, plus the palette layer:

1. The planner's recommendation drives the theme (variety).
2. Explicit user style words still win over the recommendation.
3. Content keywords remain a fallback (no recommendation → swiss_modern).
4. The planner schema enum actually matches the real catalog.
5. Palette layer: the planner's palette_recommendation drives the deck's
   color identity (accent / primary / chart series) on top of the theme.
6. Palette rotation: when the planner gives no palette, a stable hash of
   the user message picks one — same-topic decks differ in color.
7. Theme rotation: when NOTHING matches (no style word, no recommendation,
   no content word, no deck-type default), themes alternate by hash instead
   of every deck collapsing to electric_studio.
"""

import json

from app.services.synexia.contracts import DeckPlan
from app.services.artifacts.themes import (
    THEME_CATALOG,
    select_theme,
    resolve_theme_tokens,
)
from app.services.artifacts.palettes import PALETTE_CATALOG
from app.services.artifacts.deck_planner import _PLAN_SCHEMA


def _plan(theme: str = "", palette: str = "") -> DeckPlan:
    return DeckPlan(
        title="C5/C9 Market View",
        theme_recommendation=theme,
        palette_recommendation=palette,
        slides=[],
    )


def test_planner_recommendation_drives_theme() -> None:
    """Same user message, different recommendations -> different themes."""
    msg = "make a c5 c9 market view ppt"
    seen = set()
    for rec in ("bold_signal", "neon_cyber", "paper_and_ink", "vintage_editorial"):
        theme = select_theme(_plan(rec), msg)
        assert theme.name == rec  # recommendation wins over market keyword
        seen.add(theme.name)
    assert len(seen) == 4, "expected distinct themes per recommendation"


def test_no_recommendation_falls_back_to_content_keyword() -> None:
    """No planner pick -> market content keyword still resolves business."""
    theme = select_theme(_plan(), "make a c5 c9 market view ppt")
    assert theme.name == "swiss_modern"


def test_user_style_keyword_wins_over_recommendation() -> None:
    """Explicit style word beats the planner's recommendation."""
    theme = select_theme(_plan("paper_and_ink"), "make a tech pitch deck")
    assert theme.name == "neon_cyber"


def test_structured_path_honors_recommendation() -> None:
    """resolve_theme_tokens (layout engine) honors the recommendation too."""
    tokens = resolve_theme_tokens(
        {}, plan=_plan("neon_cyber"), user_message="make a market ppt"
    )
    assert tokens["slide_bg"] == "#0a0a1a"  # neon_cyber dark background


def test_planner_schema_enum_matches_catalog() -> None:
    """Every theme name the planner may emit exists in the catalog."""
    enum = _PLAN_SCHEMA["properties"]["theme_recommendation"]["enum"]
    assert set(enum) == set(THEME_CATALOG.keys()), (
        f"schema enum {set(enum)} != catalog {set(THEME_CATALOG.keys())}"
    )
    assert "zhanlu-blue" not in enum, "stale legacy theme name must not be offered"


# ---------------------------------------------------------------------------
# Palette layer (option 3): per-deck professional color identity
# ---------------------------------------------------------------------------


def test_planner_palette_recommendation_drives_color_identity() -> None:
    """Different palette picks -> different accent/primary on the same theme."""
    theme = select_theme(_plan("swiss_modern", "analytics_amber"), "market ppt")
    assert theme.color_tokens["palette_name"] == "analytics_amber"
    assert theme.color_tokens["accent"] == "#D97706"
    assert theme.color_tokens["primary"] == "#1E40AF"
    assert "chart_series" in theme.color_tokens

    other = select_theme(_plan("swiss_modern", "luxury_gold"), "market ppt")
    assert other.color_tokens["accent"] == "#A16207"
    assert other.color_tokens["accent"] != theme.color_tokens["accent"]


def test_palette_rotation_varies_same_topic_decks() -> None:
    """No planner palette -> stable hash still gives per-message color variety."""
    seen = set()
    for msg in (
        "make a c5 c9 market view ppt",
        "market view deck for q3",
        "industry outlook presentation",
    ):
        theme = select_theme(_plan("swiss_modern"), msg)
        pal = theme.color_tokens["palette_name"]
        seen.add(pal)
        # Deterministic per message: same input -> same palette.
        again = select_theme(_plan("swiss_modern"), msg)
        assert again.color_tokens["palette_name"] == pal
    assert len(seen) > 1, "expected palette rotation to vary across messages"


def test_theme_rotation_when_no_signal() -> None:
    """No style word / no rec / no content word / no deck-type -> rotate."""
    # "hello deck" has no theme signal; deck_type is empty.
    plan = DeckPlan(title="hello", deck_type="", slides=[])
    msg = "hello make me a deck about widgets"
    themes = set()
    palettes = set()
    for i, extra in enumerate(("a", "b", "c", "d", "e")):
        t = select_theme(plan, msg + " " + extra)
        themes.add(t.name)
        palettes.add(t.color_tokens["palette_name"])
    assert len(themes) > 1, "expected theme rotation to vary across messages"
    assert len(palettes) > 1, "expected palette rotation alongside themes"


def test_planner_palette_schema_enum_matches_catalog() -> None:
    """Every palette the planner may emit exists in the palette catalog."""
    enum = _PLAN_SCHEMA["properties"]["palette_recommendation"]["enum"]
    assert set(enum) == set(PALETTE_CATALOG.keys()), (
        f"palette schema enum {set(enum)} != catalog {set(PALETTE_CATALOG.keys())}"
    )


def test_structured_path_applies_palette_tokens() -> None:
    """Layout engine tokens carry the palette's primary + chart series."""
    tokens = resolve_theme_tokens(
        {}, plan=_plan("neon_cyber", "finance_dark"), user_message="market ppt"
    )
    assert tokens["primary"] == "#0F172A"  # finance_dark primary
    assert tokens["palette_name"] == "finance_dark"
    assert "chart_series" in tokens and len(tokens["chart_series"]) >= 4
    assert tokens["slide_bg"] == "#0a0a1a"  # theme surface untouched
