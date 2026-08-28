"""Theme catalog for the HTML design renderer (Phase 4).

Twelve curated visual presets, transcribed from
``backend/skills/ppt_skills/frontend-slides-main/STYLE_PRESETS.md``
with normalized color tokens.  Each preset is a ``ThemePreset``
dataclass: name, fonts, color tokens, and signature elements.

When ``settings.HTML_DESIGN_THEMES`` is non-empty, only the listed
presets are available — the others are filtered out at lookup time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class ThemePreset:
    """One visual preset the HTML renderer can apply.

    ``color_tokens`` always includes the three keys ``bg_primary``,
    ``text_primary``, ``accent`` so callers can build a CSS ``:root``
    without per-theme conditionals.  Theme-specific keys
    (e.g. ``card_bg`` for Bold Signal) live alongside the canonical
    three.
    """
    name: str
    display_name: str
    font_display: str
    font_body: str
    color_tokens: Dict[str, str]
    signature_elements: List[str] = field(default_factory=list)
    layout_overrides: Dict[str, Dict] = field(default_factory=dict)


# 12 frontend-slides style presets (BIB-accurate names + colors).
_BOLD_SIGNAL = ThemePreset(
    name="bold_signal",
    display_name="Bold Signal",
    font_display="Archivo Black",
    font_body="Space Grotesk",
    color_tokens={
        "bg_primary": "#1a1a1a",
        "bg_gradient": "linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 50%, #1a1a1a 100%)",
        "card_bg": "#FF5722",
        "text_primary": "#ffffff",
        "text_on_card": "#1a1a1a",
        "accent": "#FF5722",
    },
    signature_elements=[
        "Bold colored card as focal point",
        "Large section numbers (01, 02)",
        "Navigation breadcrumbs",
    ],
)

_ELECTRIC_STUDIO = ThemePreset(
    name="electric_studio",
    display_name="Electric Studio",
    font_display="Manrope",
    font_body="Manrope",
    color_tokens={
        "bg_dark": "#0a0a0a",
        "bg_white": "#ffffff",
        "accent_blue": "#4361ee",
        "text_dark": "#0a0a0a",
        "text_light": "#ffffff",
        "bg_primary": "#ffffff",
        "text_primary": "#0a0a0a",
        "accent": "#4361ee",
    },
    signature_elements=[
        "Two-panel vertical split (white top, blue bottom)",
        "Accent bar on panel edge",
        "Quote typography as hero element",
    ],
)

_CREATIVE_VOLTAGE = ThemePreset(
    name="creative_voltage",
    display_name="Creative Voltage",
    font_display="Syne",
    font_body="Space Grotesk",
    color_tokens={
        "bg_primary": "#0a0a0a",
        "text_primary": "#ffffff",
        "accent": "#4361ee",
        "accent_alt": "#FF5722",
    },
    signature_elements=["Split panels: electric blue left, dark right", "Script accents"],
)

_DARK_BOTANICAL = ThemePreset(
    name="dark_botanical",
    display_name="Dark Botanical",
    font_display="Fraunces",
    font_body="Inter",
    color_tokens={"bg_primary": "#1a2e1f", "text_primary": "#e8e4d8", "accent": "#7cb342"},
    signature_elements=["Botanical line illustrations", "Warm earth accents on deep green"],
)

_NOTEBOOK_TABS = ThemePreset(
    name="notebook_tabs",
    display_name="Notebook Tabs",
    font_display="Caveat",
    font_body="Work Sans",
    color_tokens={"bg_primary": "#fdf6e3", "text_primary": "#2c2417", "accent": "#d97706"},
    signature_elements=["Tabbed notebook visual metaphor", "Handwritten feel"],
)

_PASTEL_GEOMETRY = ThemePreset(
    name="pastel_geometry",
    display_name="Pastel Geometry",
    font_display="DM Serif Display",
    font_body="DM Sans",
    color_tokens={"bg_primary": "#fce7f3", "text_primary": "#1f2937", "accent": "#a78bfa"},
    signature_elements=["Soft pastel geometric shapes", "Rounded corners"],
)

_SPLIT_PASTEL = ThemePreset(
    name="split_pastel",
    display_name="Split Pastel",
    font_display="Plus Jakarta Sans",
    font_body="Plus Jakarta Sans",
    color_tokens={"bg_primary": "#fef3c7", "text_primary": "#1f2937", "accent": "#fb7185"},
    signature_elements=["Split-pane pastel color blocks"],
)

_VINTAGE_EDITORIAL = ThemePreset(
    name="vintage_editorial",
    display_name="Vintage Editorial",
    font_display="Fraunces",
    font_body="Work Sans",
    color_tokens={"bg_primary": "#f5ebd6", "text_primary": "#3a2618", "accent": "#a8492c"},
    signature_elements=["Drop caps", "Halftone treatment", "Warm cream paper background"],
)

_NEON_CYBER = ThemePreset(
    name="neon_cyber",
    display_name="Neon Cyber",
    font_display="Orbitron",
    font_body="Rajdhani",
    color_tokens={"bg_primary": "#0a0a1a", "text_primary": "#e0e7ff", "accent": "#00f5d4"},
    signature_elements=["Glow + scanline treatment", "Monospace accents"],
)

_TERMINAL_GREEN = ThemePreset(
    name="terminal_green",
    display_name="Terminal Green",
    font_display="JetBrains Mono",
    font_body="JetBrains Mono",
    color_tokens={"bg_primary": "#0a0a0a", "text_primary": "#00ff00", "accent": "#00cc00"},
    signature_elements=["Monospace everything", "Blinking cursor indicator"],
)

_SWISS_MODERN = ThemePreset(
    name="swiss_modern",
    display_name="Swiss Modern",
    font_display="Helvetica Neue",
    font_body="Helvetica Neue",
    color_tokens={"bg_primary": "#ffffff", "text_primary": "#0a0a0a", "accent": "#dc2626"},
    signature_elements=["Strict grid alignment", "Asymmetric typography", "Red accent blocks"],
)

_PAPER_AND_INK = ThemePreset(
    name="paper_and_ink",
    display_name="Paper & Ink",
    font_display="Crimson Pro",
    font_body="Source Sans 3",
    color_tokens={"bg_primary": "#fafaf9", "text_primary": "#1c1917", "accent": "#1c1917"},
    signature_elements=["Editorial body copy", "Generous margins"],
)

THEME_CATALOG: Dict[str, ThemePreset] = {
    p.name: p for p in (
        _BOLD_SIGNAL, _ELECTRIC_STUDIO, _CREATIVE_VOLTAGE, _DARK_BOTANICAL,
        _NOTEBOOK_TABS, _PASTEL_GEOMETRY, _SPLIT_PASTEL, _VINTAGE_EDITORIAL,
        _NEON_CYBER, _TERMINAL_GREEN, _SWISS_MODERN, _PAPER_AND_INK,
    )
}


def get_theme(name: str) -> ThemePreset:
    """Return the preset for ``name`` or raise ``KeyError``."""
    if name not in THEME_CATALOG:
        raise KeyError(
            f"Unknown theme: {name!r}. Available: {sorted(THEME_CATALOG)}"
        )
    return THEME_CATALOG[name]


# Default preset per deck_type.
_DECK_TYPE_DEFAULT: Dict[str, str] = {
    "data_report": "electric_studio",
    "investor_deck": "bold_signal",
    "marketing": "creative_voltage",
    "executive_brief": "paper_and_ink",
    "training": "notebook_tabs",
}

# Keyword overrides (case-insensitive substring match on user_message).
# FIRST match wins.
_KEYWORD_OVERRIDES: List[tuple] = [
    ("editorial", "vintage_editorial"),
    ("wellness", "dark_botanical"),
    ("playful", "pastel_geometry"),
    ("tech", "neon_cyber"),
    ("terminal", "terminal_green"),
    ("report", "swiss_modern"),
    ("technical", "neon_cyber"),
    ("calm", "paper_and_ink"),
    ("modern", "swiss_modern"),
    # Market / industry / business intel (EN + CJK) → business preset.
    ("market", "swiss_modern"),
    ("industry", "swiss_modern"),
    ("市场", "swiss_modern"),
    ("行业", "swiss_modern"),
    ("行情", "swiss_modern"),
]


def _available_themes() -> Dict[str, ThemePreset]:
    """Apply the deployment's ``HTML_DESIGN_THEMES`` allow-list (if any)."""
    from app.config import settings
    allowed = settings.HTML_DESIGN_THEMES or []
    if not allowed:
        return THEME_CATALOG
    return {n: p for n, p in THEME_CATALOG.items() if n in allowed}


def select_theme(plan, user_message: str = "") -> ThemePreset:
    """Map a deck plan + user message to a concrete ``ThemePreset``.

    Resolution order:
      1. Keyword overrides in ``user_message`` (first match wins).
      2. ``plan.deck_type`` → default preset table.
      3. Fall back to ``electric_studio``.

    The result is always drawn from ``settings.HTML_DESIGN_THEMES`` if that
    allow-list is configured; otherwise from the full catalog.
    """
    from app.services.synexia.contracts import DeckPlan

    available = _available_themes()

    msg = (user_message or "").lower()
    for keyword, preset_name in _KEYWORD_OVERRIDES:
        if keyword in msg and preset_name in available:
            return available[preset_name]

    deck_type = (plan.deck_type if isinstance(plan, DeckPlan) else "") or ""
    default_name = _DECK_TYPE_DEFAULT.get(deck_type.strip().lower())
    if default_name and default_name in available:
        return available[default_name]

    # Last resort: prefer ``electric_studio`` if available, else the
    # first available preset (alphabetical order for determinism).
    if "electric_studio" in available:
        return available["electric_studio"]
    if available:
        first_name = sorted(available)[0]
        return available[first_name]
    raise RuntimeError("no themes available in catalog")


__all__ = ["ThemePreset", "THEME_CATALOG", "get_theme", "select_theme"]
