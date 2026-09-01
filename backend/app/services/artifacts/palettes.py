"""Curated professional palette catalog for decks.

Each palette is transcribed from ui-ux-pro-max's WCAG-checked color database
(161 product palettes), normalized to the flat token scheme the layout engine
and HTML renderer consume.  A palette provides the COLOR IDENTITY of a deck —
primary/accent/chart-series — layered ON TOP of the structural theme (fonts,
surfaces, layout signature) so every deck gets a distinct professional look.

The palette layer is the second axis of deck variety:

  * Theme  = structure + typography + mood (12 presets in themes.py)
  * Palette = color identity (primary / accent / chart series)

A market deck can therefore render with an Analytics-blue identity on a
swiss_modern grid, or a Luxury-gold identity on paper_and_ink — two different
decks, two different professional color stories, without inventing new layouts.

``PALETTE_CATALOG`` maps palette name -> ``PalettePreset``.  The planner
recommends one via ``DeckPlan.palette_recommendation``; when the planner gives
no recommendation the resolver falls back to a stable hash of the user message
so even same-topic decks alternate color identities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class PalettePreset:
    """One professional color identity for a deck.

    ``chart_series`` is the ordered series palette charts cycle through
    (matches the brand-palette order the layout engine uses).  ``is_dark``
    hints whether the palette was designed for a dark surface — used only to
    keep text legible when the theme surface is light.
    """

    name: str
    display_name: str
    primary: str
    accent: str
    secondary: str = ""
    chart_series: List[str] = field(default_factory=list)
    is_dark: bool = False
    mood: str = ""


# ---------------------------------------------------------------------------
# Curated palette set — 16 professional identities transcribed from the
# ui-ux-pro-max color DB (WCAG-checked primary/accent pairs).
# ---------------------------------------------------------------------------

_PALETTES: List[PalettePreset] = [
    PalettePreset(
        name="saas_blue",
        display_name="SaaS Blue",
        primary="#2563EB",
        accent="#EA580C",
        secondary="#3B82F6",
        chart_series=["#2563EB", "#3B82F6", "#EA580C", "#64748B", "#10B981", "#8B5CF6"],
        mood="trust, clarity, B2B software",
    ),
    PalettePreset(
        name="micro_indigo",
        display_name="Micro Indigo",
        primary="#6366F1",
        accent="#059669",
        secondary="#818CF8",
        chart_series=["#6366F1", "#818CF8", "#059669", "#F59E0B", "#0EA5E9", "#F43F5E"],
        mood="modern, focused, startup energy",
    ),
    PalettePreset(
        name="commerce_green",
        display_name="Commerce Green",
        primary="#059669",
        accent="#EA580C",
        secondary="#10B981",
        chart_series=["#059669", "#10B981", "#EA580C", "#0EA5E9", "#8B5CF6", "#64748B"],
        mood="growth, commerce, conversion",
    ),
    PalettePreset(
        name="luxury_gold",
        display_name="Luxury Gold",
        primary="#1C1917",
        accent="#A16207",
        secondary="#44403C",
        chart_series=["#1C1917", "#A16207", "#D97706", "#78716C", "#57534E", "#B45309"],
        mood="premium, elegant, high-end",
    ),
    PalettePreset(
        name="b2b_navy",
        display_name="B2B Navy",
        primary="#0F172A",
        accent="#0369A1",
        secondary="#334155",
        chart_series=["#0F172A", "#0369A1", "#0EA5E9", "#64748B", "#10B981", "#F59E0B"],
        mood="professional, corporate, enterprise",
    ),
    PalettePreset(
        name="finance_dark",
        display_name="Finance Dark",
        primary="#0F172A",
        accent="#22C55E",
        secondary="#1E293B",
        chart_series=["#22C55E", "#3B82F6", "#F59E0B", "#EF4444", "#8B5CF6", "#0EA5E9"],
        is_dark=True,
        mood="financial, markets, trading",
    ),
    PalettePreset(
        name="analytics_amber",
        display_name="Analytics Amber",
        primary="#1E40AF",
        accent="#D97706",
        secondary="#3B82F6",
        chart_series=["#1E40AF", "#3B82F6", "#D97706", "#F59E0B", "#10B981", "#8B5CF6"],
        mood="data, dashboards, BI",
    ),
    PalettePreset(
        name="health_teal",
        display_name="Health Teal",
        primary="#0891B2",
        accent="#059669",
        secondary="#22D3EE",
        chart_series=["#0891B2", "#22D3EE", "#059669", "#0EA5E9", "#F59E0B", "#8B5CF6"],
        mood="care, wellbeing, life-sciences",
    ),
    PalettePreset(
        name="edu_indigo",
        display_name="Education Indigo",
        primary="#4F46E5",
        accent="#EA580C",
        secondary="#818CF8",
        chart_series=["#4F46E5", "#818CF8", "#EA580C", "#0D9488", "#F59E0B", "#8B5CF6"],
        mood="learning, academic, structured",
    ),
    PalettePreset(
        name="agency_pink",
        display_name="Creative Agency",
        primary="#EC4899",
        accent="#0891B2",
        secondary="#F472B6",
        chart_series=["#EC4899", "#F472B6", "#0891B2", "#8B5CF6", "#F59E0B", "#0D9488"],
        mood="creative, bold, marketing",
    ),
    PalettePreset(
        name="gaming_violet",
        display_name="Gaming Violet",
        primary="#7C3AED",
        accent="#F43F5E",
        secondary="#A78BFA",
        chart_series=["#7C3AED", "#A78BFA", "#F43F5E", "#F59E0B", "#0EA5E9", "#22D55E"],
        is_dark=True,
        mood="gaming, esports, entertainment",
    ),
    PalettePreset(
        name="ai_violet",
        display_name="AI Platform",
        primary="#7C3AED",
        accent="#0891B2",
        secondary="#A78BFA",
        chart_series=["#7C3AED", "#A78BFA", "#0891B2", "#22D3EE", "#F59E0B", "#10B981"],
        mood="AI, innovation, futuristic",
    ),
    PalettePreset(
        name="logistics_blue",
        display_name="Logistics Blue",
        primary="#2563EB",
        accent="#EA580C",
        secondary="#3B82F6",
        chart_series=["#2563EB", "#3B82F6", "#EA580C", "#10B981", "#F59E0B", "#64748B"],
        mood="operations, supply-chain, delivery",
    ),
    PalettePreset(
        name="energy_green",
        display_name="Energy Green",
        primary="#15803D",
        accent="#A16207",
        secondary="#22C55E",
        chart_series=["#15803D", "#22C55E", "#A16207", "#F59E0B", "#0EA5E9", "#64748B"],
        mood="energy, sustainability, industry",
    ),
    PalettePreset(
        name="travel_sky",
        display_name="Travel Sky",
        primary="#0EA5E9",
        accent="#EA580C",
        secondary="#38BDF8",
        chart_series=["#0EA5E9", "#38BDF8", "#EA580C", "#0D9488", "#8B5CF6", "#F59E0B"],
        mood="travel, tourism, hospitality",
    ),
    PalettePreset(
        name="legal_royal",
        display_name="Legal Royal",
        primary="#1E3A8A",
        accent="#B45309",
        secondary="#1E40AF",
        chart_series=["#1E3A8A", "#1E40AF", "#B45309", "#D97706", "#64748B", "#0EA5E9"],
        mood="law, governance, conservative",
    ),
]

PALETTE_CATALOG: Dict[str, PalettePreset] = {p.name: p for p in _PALETTES}


def get_palette(name: str) -> PalettePreset:
    """Return the palette for ``name`` or raise ``KeyError``."""
    if name not in PALETTE_CATALOG:
        raise KeyError(
            f"Unknown palette: {name!r}. Available: {sorted(PALETTE_CATALOG)}"
        )
    return PALETTE_CATALOG[name]


def list_palettes() -> list[dict]:
    """Catalog of selectable palettes (for a future UI style-picker)."""
    return [
        {"name": p.name, "display_name": p.display_name, "mood": p.mood}
        for p in PALETTE_CATALOG.values()
    ]


def palette_series(name: str) -> list[str]:
    """Ordered chart series colors for ``name`` (falls back to a default)."""
    try:
        return list(get_palette(name).chart_series)
    except KeyError:
        return ["#2563EB", "#3B82F6", "#EA580C", "#64748B", "#10B981", "#8B5CF6"]


__all__ = [
    "PalettePreset",
    "PALETTE_CATALOG",
    "get_palette",
    "list_palettes",
    "palette_series",
]
