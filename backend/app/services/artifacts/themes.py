"""Theme catalog for the HTML design renderer (Phase 4).

Twelve curated visual presets, transcribed from
``backend/skills/ppt_skills/frontend-slides-main/STYLE_PRESETS.md``
with normalized color tokens.  Each preset is a ``ThemePreset``
dataclass: name, fonts, color tokens, and signature elements.

When ``settings.HTML_DESIGN_THEMES`` is non-empty, only the listed
presets are available — the others are filtered out at lookup time.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List


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
    color_tokens: Dict[str, Any]
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
# FIRST match wins.  Two tiers:
#   * _STYLE_KEYWORDS — the user EXPLICITLY asked for a look (editorial,
#     tech, playful...).  These always win: the user's words beat the
#     planner's content-based recommendation.
#   * _CONTENT_KEYWORDS — topic words (market, industry, report, 市场...)
#     that hint at a register but are NOT a styling request.  These only
#     apply when the planner did NOT recommend a theme, so a market deck
#     is free to use bold_signal / neon_cyber / paper_and_ink depending on
#     the content and audience the planner chose — not forced into the
#     same white grid every time.
_STYLE_KEYWORDS: List[tuple] = [
    ("editorial", "vintage_editorial"),
    ("wellness", "dark_botanical"),
    ("playful", "pastel_geometry"),
    ("tech", "neon_cyber"),
    ("terminal", "terminal_green"),
    ("calm", "paper_and_ink"),
    ("modern", "swiss_modern"),
    ("minimal", "paper_and_ink"),
    ("bold", "bold_signal"),
    ("vibrant", "creative_voltage"),
    ("elegant", "vintage_editorial"),
    ("clean", "swiss_modern"),
    ("fun", "pastel_geometry"),
    ("dark", "dark_botanical"),
    ("hacker", "terminal_green"),
    ("cyber", "neon_cyber"),
    ("investor", "bold_signal"),
    ("pitch", "bold_signal"),
    ("路演", "bold_signal"),
    ("融资", "bold_signal"),
    ("投资", "bold_signal"),
    ("科技", "neon_cyber"),
    ("技术", "neon_cyber"),
    ("创意", "creative_voltage"),
    ("营销", "creative_voltage"),
    ("品牌", "creative_voltage"),
    ("复古", "vintage_editorial"),
    ("杂志", "vintage_editorial"),
    ("温暖", "pastel_geometry"),
    ("柔和", "pastel_geometry"),
    ("简洁", "paper_and_ink"),
    ("极简", "paper_and_ink"),
]

# Topic words that imply a business register but are NOT style requests.
_CONTENT_KEYWORDS: List[tuple] = [
    ("report", "swiss_modern"),
    ("market", "swiss_modern"),
    ("industry", "swiss_modern"),
    ("分析", "swiss_modern"),
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


def _stable_seed(text: str) -> int:
    """Deterministic 32-bit seed from a string (md5, stable across runs)."""
    return int.from_bytes(hashlib.md5((text or "").encode("utf-8")).digest()[:4], "big")


def _apply_palette(preset: ThemePreset, palette_name: str) -> ThemePreset:
    """Layer a palette's color identity onto a theme preset.

    Returns a NEW ``ThemePreset`` (the catalog presets stay immutable) whose
    color tokens carry the palette's accent/primary plus a ``chart_series``
    list for charts.  The theme's fonts/surfaces/signature elements are
    untouched — palette only swaps the color identity.
    """
    from app.services.artifacts.palettes import get_palette

    pal = get_palette(palette_name)
    tokens = dict(preset.color_tokens)
    tokens["accent"] = pal.accent
    tokens["primary"] = pal.primary
    tokens["chart_series"] = list(pal.chart_series)
    tokens["palette_name"] = pal.name
    return replace(preset, color_tokens=tokens)


def select_theme(plan, user_message: str = "") -> ThemePreset:
    """Map a deck plan + user message to a concrete ``ThemePreset``.

    Resolution order:
      1. Explicit STYLE keyword in ``user_message`` (first match wins) —
         the USER's styling words beat everything (editorial/tech/playful).
      2. ``plan.theme_recommendation`` — the theme the LLM planner chose
         for THIS deck's content.  Gives per-deck variety (a market deck
         is not forced into one look just because the word "market" is in
         the request).
      3. CONTENT keyword in ``user_message`` (market/industry/report/分析)
         — business register hint used only when the planner had no pick.
      4. ``plan.deck_type`` → default preset table.
      5. Hash rotation over the available presets (seeded by the user
         message) — same-topic decks alternate looks deterministically
         instead of every no-signal deck collapsing to electric_studio.
      6. Fall back to ``electric_studio``.

    The result is always drawn from ``settings.HTML_DESIGN_THEMES`` if that
    allow-list is configured; otherwise from the full catalog.

    After the theme is chosen, the palette layer is applied: ``plan.
    palette_recommendation`` when the planner picked one, otherwise a
    deterministic hash of the user message so even same-topic decks get
    distinct professional color identities.
    """
    from app.services.synexia.contracts import DeckPlan

    available = _available_themes()

    msg = (user_message or "").lower()
    for keyword, preset_name in _STYLE_KEYWORDS:
        if keyword in msg and preset_name in available:
            preset = available[preset_name]
            return _apply_palette_or_rotate(preset, plan, user_message)

    # The planner's own recommendation — the single biggest lever for
    # deck variety.  Only trust it when it names a REAL catalog theme.
    if isinstance(plan, DeckPlan):
        rec = (getattr(plan, "theme_recommendation", "") or "").strip().lower()
        if rec and rec in available:
            preset = available[rec]
            return _apply_palette_or_rotate(preset, plan, user_message)

    for keyword, preset_name in _CONTENT_KEYWORDS:
        if keyword in msg and preset_name in available:
            preset = available[preset_name]
            return _apply_palette_or_rotate(preset, plan, user_message)

    deck_type = (plan.deck_type if isinstance(plan, DeckPlan) else "") or ""
    default_name = _DECK_TYPE_DEFAULT.get(deck_type.strip().lower())
    if default_name and default_name in available:
        preset = available[default_name]
        return _apply_palette_or_rotate(preset, plan, user_message)

    # Hash rotation: no style word, no planner pick, no content word, no
    # deck-type default → rotate deterministically by the user message so
    # repeated "make a market ppt" requests don't all look identical.
    names = sorted(available)
    if names:
        idx = _stable_seed(user_message) % len(names)
        preset = available[names[idx]]
        return _apply_palette_or_rotate(preset, plan, user_message)

    # Last resort: prefer ``electric_studio`` if available, else the
    # first available preset (alphabetical order for determinism).
    if "electric_studio" in available:
        return available["electric_studio"]
    if available:
        first_name = sorted(available)[0]
        return available[first_name]
    raise RuntimeError("no themes available in catalog")


def _apply_palette_or_rotate(preset: ThemePreset, plan, user_message: str) -> ThemePreset:
    """Apply the palette layer: planner's pick, else hash rotation.

    Palette is the second, independent variety axis (theme = structure,
    palette = color identity).  When the planner named a real palette it
    wins; otherwise the user message is hashed to a stable palette so
    same-topic decks still differ in color while staying deterministic.
    """
    from app.services.synexia.contracts import DeckPlan
    from app.services.artifacts.palettes import PALETTE_CATALOG

    palette_name = ""
    if isinstance(plan, DeckPlan):
        palette_name = (getattr(plan, "palette_recommendation", "") or "").strip().lower()
        if palette_name not in PALETTE_CATALOG:
            palette_name = ""
    if not palette_name:
        names = sorted(PALETTE_CATALOG)
        if names:
            palette_name = names[_stable_seed(user_message) % len(names)]
    return _apply_palette(preset, palette_name)


# ---------------------------------------------------------------------------
# PPTX bridge: map a ThemePreset -> the flat token schema layout_engine reads
# ---------------------------------------------------------------------------
#
# The 12 ``ThemePreset``s above were originally wired only into the (disabled)
# HTML renderer.  The PPTX ``layout_engine`` consumes a *flat* ``theme_tokens``
# dict with a different key scheme (``primary`` / ``slide_bg`` / ``text`` /
# ``font_heading`` ...).  These helpers bridge the two so every preset becomes
# a usable PowerPoint style, and add a resolver that auto-selects a style by
# deck type / keyword (reactivating the intent behind ``_DECK_TYPE_DEFAULT``
# and ``_KEYWORD_OVERRIDES``).

import colorsys


def _hex_to_rgb(h: str) -> tuple:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb: tuple) -> str:
    return "#" + "".join(f"{max(0, min(255, int(c))):02x}" for c in rgb)


def _luminance(h: str) -> float:
    r, g, b = [c / 255.0 for c in _hex_to_rgb(h)]

    def _lin(x: float) -> float:
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _is_dark(h: str) -> bool:
    return _luminance(h) < 0.5


def _blend(a: str, b: str, t: float) -> str:
    """Return ``t`` fraction of ``b`` laid over ``a``."""
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    return _rgb_to_hex(
        (ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)
    )


# Preserved historical default look (used when no preset is selected).
_ZHANLU_BLUE_TOKENS = {
    "name": "zhanlu-blue",
    "primary": "#2563EB",
    "slide_bg": "#FFFFFF",
    "text": "#0F172A",
    "muted": "#64748B",
    "border": "#E2E8F0",
    "kpi_bg": "#F1F5F9",
    "rec_bg": "#EFF6FF",
    "finding_bg": "#F5F3FF",
    "band_bg": "#F8FAFC",
    "risk": "#DC2626",
    "risk_bg": "#FEF2F2",
    "opportunity": "#059669",
    "opp_bg": "#ECFDF5",
    "delta_up": "#059669",
    "delta_down": "#DC2626",
    "warn_accent": "#F59E0B",
    "finding_accent": "#7C3AED",
    "font_heading": "Inter",
    "font_body": "Inter",
    "font_cjk": "Microsoft YaHei",
}


def theme_preset_to_tokens(preset: "ThemePreset") -> dict:
    """Convert a ``ThemePreset`` into the flat ``theme_tokens`` dict the PPTX
    ``layout_engine`` expects, deriving a coherent light/dark surface palette
    so every preset (including near-black ones) renders cleanly.
    """
    ct = preset.color_tokens or {}
    bg = ct.get("bg_primary") or "#FFFFFF"
    text = ct.get("text_primary") or ("#0F172A" if not _is_dark(bg) else "#FFFFFF")
    accent = ct.get("accent") or "#2563EB"
    dark = _is_dark(bg)

    # Surface tints: lighter than bg on dark themes, slightly greyed on light.
    surface_target = "#FFFFFF" if dark else "#000000"
    surface = _blend(bg, surface_target, 0.08 if dark else 0.04)
    surface2 = _blend(bg, surface_target, 0.05 if dark else 0.02)
    border = _blend(bg, text, 0.16)
    muted = _blend(text, bg, 0.45)

    tokens = {
        "name": preset.name,
        "primary": accent,
        "slide_bg": bg,
        "text": text,
        "muted": muted,
        "border": border,
        "kpi_bg": surface,
        "rec_bg": surface,
        "finding_bg": surface2,
        "band_bg": surface2,
        "risk": "#DC2626",
        "risk_bg": _blend("#DC2626", bg, 0.88),
        "opportunity": "#059669",
        "opp_bg": _blend("#059669", bg, 0.88),
        "delta_up": "#059669",
        "delta_down": "#DC2626",
        "warn_accent": "#F59E0B",
        "finding_accent": accent,
        "font_heading": preset.font_display or "Inter",
        "font_body": preset.font_body or "Inter",
        "font_cjk": "Microsoft YaHei",
    }

    # Carry the palette color identity into the flat token scheme so charts
    # and accents use the deck's chosen palette (primary = palette primary,
    # finding_accent/warn_accent from the chart series when present).
    palette_name = ct.get("palette_name") or ""
    if palette_name:
        from app.services.artifacts.palettes import get_palette

        try:
            pal = get_palette(palette_name)
            series = pal.chart_series or []
            tokens["primary"] = pal.primary
            tokens["finding_accent"] = series[1] if len(series) > 1 else pal.accent
            tokens["warn_accent"] = series[2] if len(series) > 2 else tokens["warn_accent"]
            tokens["chart_series"] = list(series)
            tokens["palette_name"] = pal.name
        except KeyError:  # pragma: no cover — defensive
            pass

    return tokens


def resolve_theme_tokens(ctx: Any, *, plan: Any = None, user_message: str = "") -> dict:
    """Resolve the flat ``theme_tokens`` for a PPTX render.

    Priority:
      1. Explicit flat ``theme_tokens`` on ``ctx`` (brand kit) win as-is.
      2. A ``theme`` / ``theme_name`` name on ``ctx`` -> preset conversion.
      3. Auto-select via ``_KEYWORD_OVERRIDES`` (on ``user_message``) and
         ``_DECK_TYPE_DEFAULT`` (on ``plan.deck_type``).
      4. Default ``zhanlu-blue`` (unchanged historical look).

    The default is intentionally ``zhanlu-blue`` (not ``select_theme``'s
    ``electric_studio``) so enabling this bridge does not silently restyle
    every existing deck.
    """
    # 1) Explicit flat tokens (brand kit / caller-supplied).
    raw = (
        ctx.get("theme_tokens")
        if isinstance(ctx, dict)
        else getattr(ctx, "theme_tokens", None)
    )
    if isinstance(raw, dict) and raw and any(
        k in raw for k in ("primary", "slide_bg", "text", "accent")
    ):
        return dict(raw)

    # 2) Explicit named preset.
    name = (
        ctx.get("theme") or ctx.get("theme_name")
        if isinstance(ctx, dict)
        else getattr(ctx, "theme", None)
    )
    if name and name in THEME_CATALOG:
        return _tokens_with_palette(
            theme_preset_to_tokens(get_theme(name)), plan, user_message
        )

    # 2b) The planner's own theme recommendation (per-deck variety).
    # This is the same lever `select_theme` uses for the HTML path: the
    # LLM planner picks a theme for THIS deck's content; both renderers
    # honor it so a market deck is not always forced into one look.
    rec = (
        plan.get("theme_recommendation")
        if isinstance(plan, dict)
        else getattr(plan, "theme_recommendation", None)
    )
    rec = (rec or "").strip().lower()
    if rec and rec in THEME_CATALOG:
        return _tokens_with_palette(
            theme_preset_to_tokens(get_theme(rec)), plan, user_message
        )

    # 3) Auto-select by keyword / deck type.
    msg = (user_message or "").lower()
    auto = None
    # User's explicit style words win.
    for keyword, preset_name in _STYLE_KEYWORDS:
        if keyword in msg and preset_name in THEME_CATALOG:
            auto = preset_name
            break
    if not auto:
        deck_type = ""
        if isinstance(plan, dict):
            deck_type = (plan.get("deck_type") or "").strip().lower()
        else:
            deck_type = (getattr(plan, "deck_type", None) or "").strip().lower()
        if deck_type in _DECK_TYPE_DEFAULT:
            candidate = _DECK_TYPE_DEFAULT[deck_type]
            if candidate in THEME_CATALOG:
                auto = candidate
    if auto:
        return _tokens_with_palette(
            theme_preset_to_tokens(get_theme(auto)), plan, user_message
        )

    # Content keyword (business register) only when nothing else matched.
    for keyword, preset_name in _CONTENT_KEYWORDS:
        if keyword in msg and preset_name in THEME_CATALOG:
            return _tokens_with_palette(
                theme_preset_to_tokens(get_theme(preset_name)), plan, user_message
            )

    # 4) Default.
    tokens = dict(_ZHANLU_BLUE_TOKENS)
    return _tokens_with_palette(tokens, plan, user_message)


def _tokens_with_palette(tokens: dict, plan: Any, user_message: str) -> dict:
    """Apply the palette layer onto flat theme tokens (planner pick or hash).

    Mirrors ``select_theme``'s palette handling for the HTML path so the
    PPTX renderer gets the same per-deck color identity: ``plan.
    palette_recommendation`` wins; otherwise a stable hash of the user
    message picks one (deterministic, so same-topic decks still vary).
    """
    from app.services.artifacts.palettes import PALETTE_CATALOG, get_palette

    palette_name = ""
    if isinstance(plan, dict):
        palette_name = (plan.get("palette_recommendation") or "").strip().lower()
    else:
        palette_name = (getattr(plan, "palette_recommendation", None) or "").strip().lower()
    if palette_name not in PALETTE_CATALOG:
        palette_name = ""
    if not palette_name:
        names = sorted(PALETTE_CATALOG)
        if names:
            palette_name = names[_stable_seed(user_message) % len(names)]
    if not palette_name:
        return tokens

    try:
        pal = get_palette(palette_name)
        series = pal.chart_series or []
        out = dict(tokens)
        out["primary"] = pal.primary
        out["accent"] = pal.accent
        out["finding_accent"] = series[1] if len(series) > 1 else pal.accent
        out["warn_accent"] = series[2] if len(series) > 2 else out.get("warn_accent", "#F59E0B")
        out["chart_series"] = list(series)
        out["palette_name"] = pal.name
        return out
    except KeyError:  # pragma: no cover — defensive
        return tokens


def list_themes() -> list[dict]:
    """Catalog of selectable presets for a future UI style-picker."""
    return [
        {"name": p.name, "display_name": p.display_name}
        for p in THEME_CATALOG.values()
    ]


__all__ = [
    "ThemePreset",
    "THEME_CATALOG",
    "get_theme",
    "select_theme",
    "theme_preset_to_tokens",
    "resolve_theme_tokens",
    "list_themes",
]
