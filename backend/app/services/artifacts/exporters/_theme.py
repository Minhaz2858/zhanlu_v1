"""Deck theme resolution — maps theme-system JSON tokens to slide colors.

Loads the brand theme JSONs in ``backend/data/themes/`` (zhanlu-owned
data kept OUTSIDE the skills folder so a skill-swap can never break deck
theming; relocated from ``skills/theme-system/themes/``) and resolves
them into a flat :class:`DeckTheme` of ``RGBColor`` values the
PPTX/HTML renderers consume.

The resolver is deliberately defensive: any missing token is *derived*
from the palette so a theme JSON never has to spell out every callout
tint.  An optional ``deck`` extension block in a theme JSON overrides
the derivations with explicit hex values (``zhanlu-blue`` uses this to
guarantee byte-for-byte parity with the pre-theme exporter).

Design tokens follow the MiniMax 5-key contract
(``primary/secondary/accent/light/bg``) adapted to Zhanlu's existing
``colors.{light,dark}`` schema.  Style recipes (Sharp/Soft/Rounded/Pill)
drive corner radius + spacing density per the pptx-generator design
system.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pptx.dml.color import RGBColor


# Theme JSONs are zhanlu-owned brand data, kept OUTSIDE the skills folder
# so a skill-swap can never break deck theming. Relocated from
# skills/theme-system/themes/ to backend/data/themes/.
# _theme.py is at backend/app/services/artifacts/exporters/_theme.py
# parents[4] -> backend  ->  backend/data/themes
_THEMES_DIR = (
    Path(__file__).resolve().parents[4]
    / "data" / "themes"
)

DEFAULT_THEME = "zhanlu-blue"
DEFAULT_MODE = "light"
DEFAULT_RECIPE = "sharp"


# ---------------------------------------------------------------------------
# Color math (hex strings in/out)
# ---------------------------------------------------------------------------

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = (h or "").lstrip("#").strip()
    if len(h) == 3:  # #abc -> #aabbcc
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"invalid hex color: {h!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _to_rgbcolor(h: str) -> RGBColor:
    r, g, b = _hex_to_rgb(h)
    return RGBColor(r, g, b)


def _mix(a: str, b: str, t: float) -> str:
    """Linear blend: t=0 -> a, t=1 -> b."""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    return _rgb_to_hex(
        round(ar + (br - ar) * t),
        round(ag + (bg - ag) * t),
        round(ab + (bb - ab) * t),
    )


def _tint(base: str, white_frac: float) -> str:
    """Lighten ``base`` toward white by ``white_frac`` (0..1)."""
    return _mix(base, "#ffffff", white_frac)


def _shade(base: str, black_frac: float) -> str:
    """Darken ``base`` toward black by ``black_frac`` (0..1)."""
    return _mix(base, "#000000", black_frac)


def _darken(base: str, frac: float) -> str:
    return _shade(base, frac)


def _lighten(base: str, frac: float) -> str:
    return _tint(base, frac)


def _callout_bg(accent: str, mode: str, slide_bg: str) -> str:
    """A readable callout fill for an accent color.

    Light mode: a very light tint of the accent (accent mostly washed
    out toward white).  Dark mode: the dark slide background lifted
    slightly toward the accent so the callout reads as a subtle panel.
    """
    if mode == "dark":
        return _mix(slide_bg, accent, 0.14)
    return _tint(accent, 0.90)


# ---------------------------------------------------------------------------
# Deck theme
# ---------------------------------------------------------------------------

@dataclass
class DeckTheme:
    """Flat set of slide colors (as ``RGBColor``) the renderers need.

    Every attribute corresponds to a former ``C_*`` module constant in
    ``pptx_export.py``, so the migration is a 1:1 rename.  ``chart_palette``
    is a list of hex strings (6 steps) used by the HTML preview and
    native-chart series coloring.
    """

    # identity
    name: str = DEFAULT_THEME
    mode: str = DEFAULT_MODE
    # Human-readable theme metadata carried over from the theme JSON so the
    # deck planner / restyle logic can read tone hints (``best_for``) and a
    # description without re-parsing the file. Both default to "" and are
    # purely advisory — never used for rendering colors.
    description: str = ""
    best_for: str = ""

    # core palette
    primary: RGBColor = field(default_factory=lambda: _to_rgbcolor("#2563eb"))
    primary_dark: RGBColor = field(default_factory=lambda: _to_rgbcolor("#1d4ed8"))
    text: RGBColor = field(default_factory=lambda: _to_rgbcolor("#0f172a"))
    muted: RGBColor = field(default_factory=lambda: _to_rgbcolor("#64748b"))
    border: RGBColor = field(default_factory=lambda: _to_rgbcolor("#e2e8f0"))

    # surfaces
    slide_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#ffffff"))
    surface: RGBColor = field(default_factory=lambda: _to_rgbcolor("#f1f5f9"))
    band_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#f8fafc"))
    kpi_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#f1f5f9"))

    # callouts
    insight_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#eff6ff"))
    next_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#eff6ff"))
    rec_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#eff6ff"))
    finding_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#f5f3ff"))
    finding_accent: RGBColor = field(default_factory=lambda: _to_rgbcolor("#7c3aed"))
    warn_bg: RGBColor = field(default_factory=lambda: _to_rgbcolor("#fffbeb"))
    warn_accent: RGBColor = field(default_factory=lambda: _to_rgbcolor("#f59e0b"))

    # semantic
    delta_up: RGBColor = field(default_factory=lambda: _to_rgbcolor("#059669"))
    delta_down: RGBColor = field(default_factory=lambda: _to_rgbcolor("#dc2626"))

    # fonts (names, not colors).  Default to Inter (bundled in Docker); the
    # rendering environment falls back to Arial when Inter is unavailable.
    # ``font_cjk`` is the East Asian typeface used for Chinese runs — Inter
    # has no CJK glyphs, so without it PowerPoint falls back to SimSun/DengXian.
    font_heading: str = "Inter"
    font_body: str = "Inter"
    font_cjk: str = "Microsoft YaHei"

    # 6-step hex palette for charts
    chart_palette: list[str] = field(
        default_factory=lambda: [
            "#2563eb", "#3b82f6", "#1d4ed8", "#60a5fa", "#93c5fd", "#1e40af",
        ]
    )

    # convenience hex view (for the sandbox / preview, which take hex)
    @property
    def primary_hex(self) -> str:
        return _rgb_to_hex(self.primary[0], self.primary[1], self.primary[2])

    def as_css_vars(self, *, prefix: str = "--zl") -> dict[str, str]:
        """CSS custom-property map of the theme (P1.1 unified design tokens).

        HTML surfaces (the FINALIZE HTML report, dashboard fallback
        renderer, in-chat previews) inject these as ``:root`` variables so
        every artifact type shares ONE token source with the PPTX/DOCX
        renderers — a deck, a doc and a dashboard for the same tenant all
        read as the same brand.
        """
        h = self.as_hex_dict()
        return {
            f"{prefix}-primary": h["primary"],
            f"{prefix}-primary-dark": h["primary_dark"],
            f"{prefix}-text": h["text"],
            f"{prefix}-muted": h["muted"],
            f"{prefix}-border": h["border"],
            f"{prefix}-bg": h["slide_bg"],
            f"{prefix}-surface": h["surface"],
            f"{prefix}-band-bg": h["band_bg"],
            f"{prefix}-kpi-bg": h["kpi_bg"],
            f"{prefix}-insight-bg": h["insight_bg"],
            f"{prefix}-finding-bg": h["finding_bg"],
            f"{prefix}-finding-accent": h["finding_accent"],
            f"{prefix}-warn-bg": h["warn_bg"],
            f"{prefix}-warn-accent": h["warn_accent"],
            f"{prefix}-delta-up": h["delta_up"],
            f"{prefix}-delta-down": h["delta_down"],
            f"{prefix}-font-heading": self.font_heading,
            f"{prefix}-font-body": self.font_body,
        }

    def as_css_block(self, *, selector: str = ":root", prefix: str = "--zl") -> str:
        """A ready-to-embed ``selector { --zl-…: …; }`` CSS block."""
        lines = "; ".join(f"{k}: {v}" for k, v in self.as_css_vars(prefix=prefix).items())
        return f"{selector} {{ {lines}; }}"

    def as_hex_dict(self) -> dict[str, str]:
        """Flat hex map of every color (for passing into the sandbox via config)."""
        return {
            "primary": _rgbcolor_hex(self.primary),
            "primary_dark": _rgbcolor_hex(self.primary_dark),
            "text": _rgbcolor_hex(self.text),
            "muted": _rgbcolor_hex(self.muted),
            "border": _rgbcolor_hex(self.border),
            "slide_bg": _rgbcolor_hex(self.slide_bg),
            "surface": _rgbcolor_hex(self.surface),
            "band_bg": _rgbcolor_hex(self.band_bg),
            "kpi_bg": _rgbcolor_hex(self.kpi_bg),
            "insight_bg": _rgbcolor_hex(self.insight_bg),
            "next_bg": _rgbcolor_hex(self.next_bg),
            "rec_bg": _rgbcolor_hex(self.rec_bg),
            "finding_bg": _rgbcolor_hex(self.finding_bg),
            "finding_accent": _rgbcolor_hex(self.finding_accent),
            "warn_bg": _rgbcolor_hex(self.warn_bg),
            "warn_accent": _rgbcolor_hex(self.warn_accent),
            "delta_up": _rgbcolor_hex(self.delta_up),
            "delta_down": _rgbcolor_hex(self.delta_down),
            "chart_palette": list(self.chart_palette),
        }


def _rgbcolor_hex(c: RGBColor) -> str:
    return _rgb_to_hex(c[0], c[1], c[2])


# ---------------------------------------------------------------------------
# Style recipes (corner radius + spacing density)
# ---------------------------------------------------------------------------

@dataclass
class RecipeTokens:
    """MiniMax-style style recipe: radius + spacing density.

    ``corner_radius_in`` is the default corner radius (inches) applied to
    cards/tiles/callouts.  ``margin_in`` / ``gap_in`` are the page margin
    and inter-block gap the recipe prescribes (currently advisory — the
    exporter keeps its existing geometry for parity, but tiles/callouts
    pick up the radius).
    """

    name: str = DEFAULT_RECIPE
    corner_radius_in: float = 0.0
    margin_in: float = 0.6
    gap_in: float = 0.2
    corner_radius_px: int = 0  # for the HTML preview

    @property
    def has_radius(self) -> bool:
        return self.corner_radius_in > 0.0


_RECIPES = {
    # radius, margin, gap, px
    "sharp":   RecipeTokens("sharp",   0.0, 0.3, 0.2, 0),
    "soft":    RecipeTokens("soft",    0.08, 0.4, 0.2, 6),
    "rounded": RecipeTokens("rounded", 0.15, 0.5, 0.25, 12),
    "pill":    RecipeTokens("pill",    0.30, 0.6, 0.3, 9999),
}


def resolve_recipe(name: Optional[str]) -> RecipeTokens:
    """Return the recipe tokens for ``name`` (falls back to sharp)."""
    return _RECIPES.get((name or "").strip().lower(), _RECIPES[DEFAULT_RECIPE])


# ---------------------------------------------------------------------------
# Theme loading + resolution
# ---------------------------------------------------------------------------

def _load_theme_json(name: str) -> Optional[dict]:
    path = _THEMES_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_themes() -> list[dict]:
    """Catalog of available themes for the theme-picker API.

    Returns ``[{name, description, best_for, swatch}]`` where ``swatch``
    is a 4-hex preview of [primary, secondary, surface, background].
    """
    out = []
    if not _THEMES_DIR.exists():
        return out
    for path in sorted(_THEMES_DIR.glob("*.json")):
        try:
            tj = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        light = tj.get("colors", {}).get("light", {})
        out.append({
            "name": tj.get("name", path.stem),
            "description": tj.get("description", ""),
            "best_for": tj.get("best_for", []),
            "mode": "light",
            "swatch": [
                light.get("primary", "#2563eb"),
                light.get("secondary", "#1d4ed8"),
                light.get("surface", "#f1f5f9"),
                light.get("background", "#ffffff"),
            ],
        })
    return out


# Alias map so human-typed / LLM-emitted theme tokens resolve to a real file.
_THEME_ALIASES: dict[str, str] = {
    "zhanlu-blue": "zhanlu-blue",
    "zhanlu-dark": "zhanlu-dark",
    "blue": "zhanlu-blue",
    "default": "zhanlu-blue",
    "dark": "zhanlu-dark",
    "brand": "zhanlu-blue",
    "midnight": "midnight-navy",
    "navy": "midnight-navy",
    "midnight-navy": "midnight-navy",
    "forest": "forest-green",
    "forest-green": "forest-green",
    "green": "forest-green",
    "sunset": "sunset-orange",
    "sunset-orange": "sunset-orange",
    "orange": "sunset-orange",
    "royal": "royal-purple",
    "royal-purple": "royal-purple",
    "purple": "royal-purple",
    "teal": "teal-cyan",
    "teal-cyan": "teal-cyan",
    "cyan": "teal-cyan",
    "rose": "rose-pink",
    "rose-pink": "rose-pink",
    "pink": "rose-pink",
    "slate": "slate-gray",
    "slate-gray": "slate-gray",
    "gray": "slate-gray",
    "grey": "slate-gray",
    "amber": "amber-gold",
    "amber-gold": "amber-gold",
    "gold": "amber-gold",
    "crimson": "crimson-red",
    "crimson-red": "crimson-red",
    "red": "crimson-red",
    "emerald": "emerald-deep",
    "emerald-deep": "emerald-deep",
    "mono": "mono-ink",
    "mono-ink": "mono-ink",
    "ink": "mono-ink",
}


def list_theme_names() -> list[str]:
    """Return the canonical theme file names (no aliases)."""
    return sorted(p.stem for p in _THEMES_DIR.glob("*.json"))


def validate_theme_name(name: Optional[str]) -> str:
    """Resolve a user/LLM theme string to a real theme name.

    Accepts aliases and case-insensitive matches against BOTH catalogs:
    the legacy JSON-file catalog (``_theme.py``, zhanlu-blue / sunset /
    midnight...) AND the 12-ThemePreset renderer catalog (``themes.py``,
    neon_cyber / bold_signal / swiss_modern...). The deck-edit tools use
    this so a conversation like "make it techy/dark" (agent passes
    ``neon_cyber``, a real renderer theme) is not refused. Raises
    ``ValueError`` with the full available list when the name cannot be
    resolved, so callers (e.g. ``restyle_deck``) can surface a helpful error.
    """
    if not name:
        raise ValueError("theme name is required")
    key = name.strip().lower()
    if key in _THEME_ALIASES:
        return _THEME_ALIASES[key]
    if key in list_theme_names():
        return key
    # Case-insensitive exact file match (legacy catalog).
    for nm in list_theme_names():
        if nm.lower() == key:
            return nm
    # Renderer ThemePreset catalog — the catalog the HTML/structured
    # renderers actually resolve against (2026-08-29 integration fix).
    try:
        from app.services.artifacts.themes import THEME_CATALOG
        for preset_name in THEME_CATALOG:
            if preset_name.lower() == key:
                return preset_name
    except Exception:
        pass
    available = ", ".join(sorted(set(_THEME_ALIASES.values())))
    raise ValueError(
        f"unknown theme '{name}'. Available themes: {available}"
    )


def load_theme(name: Optional[str], mode: str = DEFAULT_MODE) -> DeckTheme:
    """Resolve a theme name (or token dict) into a :class:`DeckTheme`.

    Unknown names / load failures fall back to ``zhanlu-blue`` so the
    exporter never hard-fails on a bad theme string.  ``mode`` selects
    the ``light``/``dark`` color set inside the JSON.
    """
    name = (name or DEFAULT_THEME).strip() or DEFAULT_THEME
    mode = "dark" if (mode or "").strip().lower() == "dark" else "light"

    tj = _load_theme_json(name) or _load_theme_json(DEFAULT_THEME)
    if tj is None:
        return DeckTheme(name=DEFAULT_THEME, mode=mode)  # baked defaults

    colors = tj.get("colors", {}).get(mode, {}) or tj.get("colors", {}).get("light", {})
    deck = tj.get("deck", {}) or {}
    fonts = tj.get("fonts", {}) or {}

    def c(key: str, fallback: str) -> str:
        return (colors.get(key) or fallback).strip()

    primary = c("primary", "#2563eb")
    secondary = c("secondary", "#1d4ed8")
    surface = c("surface", "#f1f5f9")
    background = c("background", "#ffffff")
    text = c("text", "#0f172a")
    text_muted = c("text_muted", "#64748b")
    border = c("border", "#e2e8f0")
    success = c("success", "#059669")
    warning = c("warning", "#f59e0b")
    error = c("error", "#dc2626")
    info = c("info", "#0ea5e9")

    # derived (overridable via the deck block)
    primary_dark = deck.get("primary_dark") or _darken(primary, 0.10)
    band_bg = deck.get("band_bg") or _mix(background, surface, 0.5)
    kpi_bg = deck.get("kpi_bg") or surface

    insight_bg = deck.get("insight_bg") or _callout_bg(primary, mode, background)
    next_bg = deck.get("next_bg") or insight_bg
    rec_bg = deck.get("rec_bg") or insight_bg

    finding_accent = deck.get("finding_accent") or secondary or info or primary
    finding_bg = deck.get("finding_bg") or _callout_bg(finding_accent, mode, background)

    warn_accent = deck.get("warn_accent") or warning
    warn_bg = deck.get("warn_bg") or _callout_bg(warn_accent, mode, background)

    chart_palette = deck.get("chart_palette") or _derive_chart_palette(primary, secondary, info)

    return DeckTheme(
        name=name,
        mode=mode,
        description=(tj.get("description") or "") if isinstance(tj.get("description"), str) else "",
        best_for=(
            ", ".join(tj["best_for"])
            if isinstance(tj.get("best_for"), list)
            else (tj.get("best_for") or "")
        ),
        primary=_to_rgbcolor(primary),
        primary_dark=_to_rgbcolor(primary_dark),
        text=_to_rgbcolor(text),
        muted=_to_rgbcolor(text_muted),
        border=_to_rgbcolor(border),
        slide_bg=_to_rgbcolor(background),
        surface=_to_rgbcolor(surface),
        band_bg=_to_rgbcolor(band_bg),
        kpi_bg=_to_rgbcolor(kpi_bg),
        insight_bg=_to_rgbcolor(insight_bg),
        next_bg=_to_rgbcolor(next_bg),
        rec_bg=_to_rgbcolor(rec_bg),
        finding_bg=_to_rgbcolor(finding_bg),
        finding_accent=_to_rgbcolor(finding_accent),
        warn_bg=_to_rgbcolor(warn_bg),
        warn_accent=_to_rgbcolor(warn_accent),
        delta_up=_to_rgbcolor(success),
        delta_down=_to_rgbcolor(error),
        font_heading=fonts.get("heading", "Inter"),
        font_body=fonts.get("body", "Inter"),
        font_cjk=fonts.get("cjk", "Microsoft YaHei"),
        chart_palette=chart_palette,
    )


def _derive_chart_palette(primary: str, secondary: str, info: str) -> list[str]:
    """6-step chart palette from the theme's key colors."""
    return [
        primary,
        _lighten(primary, 0.20),
        _darken(primary, 0.15),
        _lighten(primary, 0.45),
        _lighten(primary, 0.65),
        _darken(primary, 0.30),
    ]


def theme_from_hex_dict(tokens: dict) -> DeckTheme:
    """Reconstruct a DeckTheme from a flat hex dict (sandbox side).

    The sandbox cannot import this module or read the skills dir, so the
    orchestrator ships ``DeckTheme.as_hex_dict()`` through ``config`` and
    the sandbox rebuilds the colors with this helper.

    An optional ``fonts`` sub-dict (``{"heading": ..., "body": ...}``)
    overrides the default font names — brand kits use this to carry the
    customer's typeface through to every renderer.
    """
    g = (tokens or {}).get
    fonts = g("fonts") or {}
    return DeckTheme(
        name=g("name", "sandbox"),
        primary=_to_rgbcolor(g("primary", "#2563eb")),
        primary_dark=_to_rgbcolor(g("primary_dark", "#1d4ed8")),
        text=_to_rgbcolor(g("text", "#0f172a")),
        muted=_to_rgbcolor(g("muted", "#64748b")),
        border=_to_rgbcolor(g("border", "#e2e8f0")),
        slide_bg=_to_rgbcolor(g("slide_bg", "#ffffff")),
        surface=_to_rgbcolor(g("surface", "#f1f5f9")),
        band_bg=_to_rgbcolor(g("band_bg", "#f8fafc")),
        kpi_bg=_to_rgbcolor(g("kpi_bg", "#f1f5f9")),
        insight_bg=_to_rgbcolor(g("insight_bg", "#eff6ff")),
        next_bg=_to_rgbcolor(g("next_bg", "#eff6ff")),
        rec_bg=_to_rgbcolor(g("rec_bg", "#eff6ff")),
        finding_bg=_to_rgbcolor(g("finding_bg", "#f5f3ff")),
        finding_accent=_to_rgbcolor(g("finding_accent", "#7c3aed")),
        warn_bg=_to_rgbcolor(g("warn_bg", "#fffbeb")),
        warn_accent=_to_rgbcolor(g("warn_accent", "#f59e0b")),
        delta_up=_to_rgbcolor(g("delta_up", "#059669")),
        delta_down=_to_rgbcolor(g("delta_down", "#dc2626")),
        font_heading=(fonts.get("heading") or "Inter") if isinstance(fonts, dict) else "Inter",
        font_body=(fonts.get("body") or "Inter") if isinstance(fonts, dict) else "Inter",
        font_cjk=(fonts.get("cjk") or "Microsoft YaHei") if isinstance(fonts, dict) else "Microsoft YaHei",
        chart_palette=list(g("chart_palette") or _derive_chart_palette("#2563eb", "#1d4ed8", "#0ea5e9")),
    )


def theme_from_brand_kit(kit: Optional[dict]) -> Optional[DeckTheme]:
    """Resolve a workspace brand kit (see ``artifacts/brand_kit.py``) to a
    DeckTheme, or None when no kit is set.

    Kept here (not in brand_kit.py) so the theme module owns all
    DeckTheme construction and brand_kit.py stays a pure storage/extract
    layer with no python-pptx dependency.
    """
    if not kit:
        return None
    from app.services.artifacts.brand_kit import brand_kit_to_theme_tokens

    try:
        tokens = brand_kit_to_theme_tokens(kit)
        theme = theme_from_hex_dict(tokens)
        fonts = tokens.get("fonts") or {}
        if fonts.get("heading"):
            theme.font_heading = fonts["heading"]
        if fonts.get("body"):
            theme.font_body = fonts["body"]
        return theme
    except Exception:
        return None


def resolve_ctx_theme(ctx) -> DeckTheme:
    """Single theme-resolution entry point for every renderer.

    Precedence:
      1. ``ctx.theme_tokens`` — a flat hex-token dict (e.g. resolved from
         the tenant's brand kit by ExportService).  Highest priority.
      2. ``ctx.theme`` / ``ctx.mode`` — a vendored theme name.

    Renderers should call this instead of ``load_theme`` directly so the
    brand-kit path works identically across pptx / docx / html.
    """
    tokens = getattr(ctx, "theme_tokens", None)
    if isinstance(tokens, dict) and tokens:
        try:
            return theme_from_hex_dict(tokens)
        except Exception:
            pass  # fall through to the named theme — never hard-fail
    return load_theme(getattr(ctx, "theme", None), getattr(ctx, "mode", DEFAULT_MODE))


__all__ = [
    "DeckTheme",
    "RecipeTokens",
    "load_theme",
    "resolve_ctx_theme",
    "resolve_recipe",
    "list_themes",
    "list_theme_names",
    "validate_theme_name",
    "theme_from_hex_dict",
    "theme_from_brand_kit",
    "DEFAULT_THEME",
    "DEFAULT_MODE",
    "DEFAULT_RECIPE",
]
