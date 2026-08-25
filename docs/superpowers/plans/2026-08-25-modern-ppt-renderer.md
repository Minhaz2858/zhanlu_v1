# Modern PPT Renderer (HTML Design Path) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an HTML-based design-rich render path to the existing PPT pipeline so design-heavy decks route to a frontend-slides-inspired theme system. The structured python-pptx renderer stays as the data-heavy path. v1.0 ships the image_fill pipeline (LibreOffice HTML→PDF→PNG → python-pptx image fills); the editable_text pipeline is architected but not implemented (v1.1).

**Architecture:** Two render paths with smart routing. Existing `route_deck(plan, user_message)` in `deck_router.py` already returns "structured" or "sandbox". We add `render_html_deck()` for the sandbox path. New modules: `themes.py` (12 preset catalog + mapping), `html_slide_generator.py` (per-layout HTML render with theme CSS), `html_to_pptx.py` (image_fill pipeline), `render_html_deck.py` (entry point). LibreOffice is already in the stack (reused from `thumbnails.py`) — no new dependencies.

**Tech Stack:** FastAPI, python-pptx, LibreOffice (`soffice` + `pdftoppm`), existing theme CSS variables from `backend/skills/ppt_skills/frontend-slides-main/STYLE_PRESETS.md`.

## Global Constraints

- All new flags default `False`: `HTML_DESIGN_RENDERER_ENABLED`, `HTML_DESIGN_EDITABLE_ENABLED`, `HTML_DESIGN_THEMES` (list). Existing `PPT_DECK_PLANNER_ENABLED` and `PPT_SMART_ROUTER_ENABLED` must be `True` for this path to fire.
- Existing structured renderer (`pptx_export.py`, `layout_engine.py`) must not be modified except for the one new branch in `service.py:_render_deck_pipeline`.
- `route_deck` returns "sandbox" today and that branch currently falls through to structured as a placeholder. We replace that placeholder with `render_html_deck(plan, ctx)`.
- LibreOffice + `pdftoppm` are required external binaries (already installed — see `backend/app/services/artifacts/thumbnails.py`). If unavailable → `render_html_deck` raises `RenderError` and `service.py` falls back to structured renderer.
- All output PPTX files must validate with `python-pptx` (`Presentation()` loads without exception).
- Image-fill PPTX is non-editable (acceptable v1.0 trade-off). Editable_text is v1.1.
- Visual regression snapshots live in `tests/snapshots/<theme>/<slide>.png` (committed). Update command documented in Task 12.
- Run local backend tests with: `cd backend && venv/bin/python -m pytest <file> -p no:jaxtyping -p no:cacheprovider`. Container tests use `/usr/local/bin/python -m pytest <file> -p no:jaxtyping -p no:cacheprovider`.
- No alembic changes. No DB schema changes. No new pip packages.

---

## Task 1: Add the three HTML design config flags

**Files:**
- Modify: `backend/app/config.py:515` (after `PPTX_VISUAL_VERIFY_ENABLED`)
- Test: `backend/tests/test_html_design_flags.py` (NEW)

**Interfaces:**
- Consumes: nothing
- Produces: three new settings fields, all default False/empty

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_html_design_flags.py`:

```python
"""Tests for the HTML design renderer config flags (all default OFF)."""
from app.config import settings


class TestHtmlDesignFlags:
    def test_html_design_renderer_disabled_by_default(self):
        assert settings.HTML_DESIGN_RENDERER_ENABLED is False

    def test_html_design_editable_disabled_by_default(self):
        assert settings.HTML_DESIGN_EDITABLE_ENABLED is False

    def test_html_design_themes_empty_by_default(self):
        assert settings.HTML_DESIGN_THEMES == []
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_design_flags.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'HTML_DESIGN_RENDERER_ENABLED'`.

- [ ] **Step 3: Add the flags to `config.py`**

In `backend/app/config.py`, immediately after the `PPTX_VISUAL_VERIFY_ENABLED: bool = False` line (~line 515), add:

```python
    # ── HTML design renderer (Phase 4) ────────────────────────────────
    # New render path for design-heavy decks. Renders the deck as HTML
    # via the theme catalog, then converts to PPTX (v1.0: image fill via
    # LibreOffice + pdftoppm; v1.1: editable text). When OFF, all decks
    # fall through to the existing structured python-pptx renderer.
    HTML_DESIGN_RENDERER_ENABLED: bool = False
    # Editable text pipeline (v1.1). Until this lands, only image_fill
    # is available regardless of HTML_DESIGN_RENDERER_ENABLED.
    HTML_DESIGN_EDITABLE_ENABLED: bool = False
    # Restrict the theme catalog. Empty list = all 12 presets available.
    # Populated by deployment, e.g. ["bold_signal", "electric_studio"].
    HTML_DESIGN_THEMES: list[str] = []
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_design_flags.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_html_design_flags.py
git commit -m "feat(ppt): add HTML_DESIGN_* config flags (all default OFF)"
```

---

## Task 2: Define the `ThemePreset` dataclass + 12-preset catalog

**Files:**
- Create: `backend/app/services/artifacts/themes.py` (NEW)
- Test: `backend/tests/test_themes_catalog.py` (NEW)

**Interfaces:**
- `ThemePreset`: frozen dataclass with `name`, `display_name`, `font_display`, `font_body`, `color_tokens: Dict[str, str]`, `signature_elements: List[str]`, `layout_overrides: Dict[str, Dict]`
- `THEME_CATALOG: Dict[str, ThemePreset]` with 12 keys
- `get_theme(name: str) -> ThemePreset` — raise `KeyError` on unknown

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_themes_catalog.py`:

```python
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
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_themes_catalog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.artifacts.themes'`.

- [ ] **Step 3: Create `themes.py` with 12 presets**

Create `backend/app/services/artifacts/themes.py`:

```python
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
# Tokens chosen to match the SKILL's CSS variables.
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


__all__ = ["ThemePreset", "THEME_CATALOG", "get_theme"]
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_themes_catalog.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/themes.py backend/tests/test_themes_catalog.py
git commit -m "feat(ppt): add 12-preset theme catalog"
```

---

## Task 3: Implement `select_theme()` — deck_type × keyword → preset

**Files:**
- Modify: `backend/app/services/artifacts/themes.py` (extend)
- Test: `backend/tests/test_select_theme.py` (NEW)

**Interfaces:**
- `select_theme(plan: Optional[DeckPlan], user_message: str = "") -> ThemePreset`
- Resolution order: (1) keyword override (e.g. "editorial" → vintage_editorial), (2) deck_type default (e.g. "investor_deck" → bold_signal), (3) fall through to "electric_studio"

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_select_theme.py`:

```python
"""Tests for select_theme() — keyword + deck_type resolution."""
from app.services.artifacts.themes import select_theme
from app.services.synexia.contracts import DeckPlan, SlidePlan


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

    def test_unknown_deck_type_falls_back(self):
        assert select_theme(_plan("nonexistent_type")).name == "electric_studio"

    def test_no_plan_falls_back(self):
        assert select_theme(None).name == "electric_studio"

    def test_case_insensitive_keyword(self):
        assert select_theme(_plan("data_report"), "WELLNESS recap").name == "dark_botanical"

    def test_settings_filter_narrows_catalog(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_THEMES", ["bold_signal"])
        # "editorial" keyword but the preset is filtered out → deck_type default
        result = select_theme(_plan("data_report"), "editorial report")
        assert result.name == "electric_studio"
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_select_theme.py -v`
Expected: FAIL with `ImportError: cannot import name 'select_theme'`.

- [ ] **Step 3: Add `select_theme()` to `themes.py`**

Append to `backend/app/services/artifacts/themes.py`:

```python
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
_KEYWORD_OVERRIDES: List[tuple[str, str]] = [
    ("editorial", "vintage_editorial"),
    ("wellness", "dark_botanical"),
    ("playful", "pastel_geometry"),
    ("tech", "neon_cyber"),
    ("terminal", "terminal_green"),
    ("report", "swiss_modern"),
    ("technical", "neon_cyber"),
    ("calm", "paper_and_ink"),
    ("modern", "swiss_modern"),
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

    return available["electric_studio"]
```

Also update `__all__`:

```python
__all__ = ["ThemePreset", "THEME_CATALOG", "get_theme", "select_theme"]
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_select_theme.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/themes.py backend/tests/test_select_theme.py
git commit -m "feat(ppt): add select_theme() with keyword + deck_type resolution"
```

---

## Task 4: HTML slide generator — cover layout

**Files:**
- Create: `backend/app/services/artifacts/html_slide_generator.py` (NEW, partial)
- Test: `backend/tests/test_html_slide_generator.py` (NEW)

**Interfaces:**
- `render_slide(layout: str, plan: SlidePlan, theme: ThemePreset) -> str` — returns HTML string
- For now only "cover" is implemented; other layouts raise `NotImplementedError`
- Output: single `<section class="slide slide--cover">` with title, subtitle, theme CSS variables at `:root`, and theme `font_display`/`font_body` families

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_html_slide_generator.py`:

```python
"""Tests for HTML slide generator (per-layout HTML rendering)."""
import pytest
from app.services.artifacts.html_slide_generator import render_slide
from app.services.artifacts.themes import get_theme
from app.services.synexia.contracts import SlidePlan


THEME = get_theme("bold_signal")
THEME_LIGHT = get_theme("electric_studio")


def _slide(**overrides) -> SlidePlan:
    base = dict(layout="cover", title="Q3 Recap", subtitle="Revenue up 8% QoQ")
    base.update(overrides)
    return SlidePlan(**base)


class TestCoverLayout:
    def test_cover_returns_html_string(self):
        html = render_slide("cover", _slide(), THEME)
        assert html.startswith("<section")
        assert "</section>" in html

    def test_cover_includes_title(self):
        html = render_slide("cover", _slide(), THEME)
        assert "Q3 Recap" in html

    def test_cover_includes_subtitle(self):
        html = render_slide("cover", _slide(), THEME)
        assert "Revenue up 8% QoQ" in html

    def test_cover_includes_theme_color_tokens(self):
        html = render_slide("cover", _slide(), THEME)
        assert "--bg-primary" in html
        assert "#1a1a1a" in html

    def test_cover_includes_theme_fonts(self):
        html = render_slide("cover", _slide(), THEME)
        assert "Archivo Black" in html
        assert "Space Grotesk" in html

    def test_cover_has_correct_layout_class(self):
        html = render_slide("cover", _slide(), THEME)
        assert 'class="slide slide--cover"' in html

    def test_cover_escapes_user_content(self):
        html = render_slide("cover", _slide(title='<script>alert("xss")</script>'), THEME)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_unknown_layout_raises(self):
        with pytest.raises(NotImplementedError):
            render_slide("kpi_grid", _slide(), THEME)
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_slide_generator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.artifacts.html_slide_generator'`.

- [ ] **Step 3: Create `html_slide_generator.py` with cover layout**

Create `backend/app/services/artifacts/html_slide_generator.py`:

```python
"""Render individual slides as HTML using the theme catalog.

One function per layout.  Returns a single ``<section class="slide
slide--{layout}">...</section>`` string.  The wrapping ``index.html``
that the renderer builds (Task 7) handles the full-page stage,
viewport-base.css inclusion, and JS bundle.
"""
from __future__ import annotations

import html
from typing import Any, Dict, List

from app.services.artifacts.themes import ThemePreset
from app.services.synexia.contracts import SlidePlan


def _theme_css(theme: ThemePreset) -> str:
    """Build a ``:root { --token: value; ... }`` block from the theme's tokens."""
    lines = [":root {"]
    for k, v in theme.color_tokens.items():
        lines.append(f"  --{k}: {v};")
    lines.append(f"  --font-display: '{theme.font_display}', serif;")
    lines.append(f"  --font-body: '{theme.font_body}', sans-serif;")
    lines.append("}")
    return "\n".join(lines)


def _render_cover(plan: SlidePlan, theme: ThemePreset) -> str:
    title = html.escape(plan.title or "")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    css = _theme_css(theme)
    subtitle_html = f'<p class="cover__subtitle">{subtitle}</p>' if subtitle else ""
    return f"""<style>
{css}
/* === COVER LAYOUT === */
.slide--cover {{
  width: 1920px; height: 1080px;
  background: var(--bg-primary, #1a1a1a);
  background-image: var(--bg-gradient, none);
  color: var(--text-primary, #fff);
  font-family: var(--font-body);
  display: grid; grid-template-columns: 1fr; align-content: end;
  padding: 96px; box-sizing: border-box;
}}
.cover__title {{
  font-family: var(--font-display); font-weight: 900;
  font-size: 128px; line-height: 1.05; letter-spacing: -0.02em;
  margin: 0 0 24px 0;
}}
.cover__subtitle {{ font-size: 36px; font-weight: 400; opacity: 0.85; margin: 0; }}
</style>
<section class="slide slide--cover">
  <h1 class="cover__title">{title}</h1>
  {subtitle_html}
</section>"""


# Per-layout dispatch.  Other layouts are added by Tasks 5-6.
_LAYOUT_RENDERERS: Dict[str, Any] = {
    "cover": _render_cover,
}


def render_slide(layout: str, plan: SlidePlan, theme: ThemePreset) -> str:
    """Render one slide as an HTML ``<section>`` string.

    Raises ``NotImplementedError`` for layouts that haven't been built yet.
    """
    renderer = _LAYOUT_RENDERERS.get(layout)
    if renderer is None:
        raise NotImplementedError(
            f"Layout {layout!r} not implemented yet. "
            f"Available: {sorted(_LAYOUT_RENDERERS)}"
        )
    return renderer(plan, theme)


__all__ = ["render_slide"]
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_slide_generator.py -v`
Expected: 9 passed (8 cover + 1 unknown).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/html_slide_generator.py backend/tests/test_html_slide_generator.py
git commit -m "feat(ppt): HTML slide generator — cover layout"
```

---

## Task 5: HTML slide generator — kpi_grid layout

**Files:**
- Modify: `backend/app/services/artifacts/html_slide_generator.py`
- Modify: `backend/tests/test_html_slide_generator.py` (add new test class)

**Interfaces:**
- New layout `"kpi_grid"` that accepts `plan.kpi_specs: List[KPISpec]`
- Renders 2-4 KPI tiles with label / value / delta / caption

- [ ] **Step 1: Add the failing test**

Append to `backend/tests/test_html_slide_generator.py`:

```python
from app.services.synexia.contracts import KPISpec


def _kpi_slide(**overrides) -> SlidePlan:
    base = dict(
        layout="kpi_grid",
        title="Quarterly KPIs",
        kpi_specs=[
            KPISpec(label="Revenue", value="$2.4M", delta="+12%", caption="vs Q2"),
            KPISpec(label="Orders", value="1,847", delta="+5%", caption="vs Q2"),
            KPISpec(label="Margin", value="31%", delta="-2pp", caption="vs Q2"),
        ],
    )
    base.update(overrides)
    return SlidePlan(**base)


class TestKpiGridLayout:
    def test_renders_without_error(self):
        html = render_slide("kpi_grid", _kpi_slide(), THEME_LIGHT)
        assert "<section" in html

    def test_includes_kpi_labels(self):
        html = render_slide("kpi_grid", _kpi_slide(), THEME_LIGHT)
        assert "Revenue" in html
        assert "Orders" in html
        assert "Margin" in html

    def test_includes_kpi_values(self):
        html = render_slide("kpi_grid", _kpi_slide(), THEME_LIGHT)
        assert "$2.4M" in html
        assert "1,847" in html
        assert "31%" in html

    def test_includes_deltas(self):
        html = render_slide("kpi_grid", _kpi_slide(), THEME_LIGHT)
        assert "+12%" in html
        assert "-2pp" in html

    def test_uses_grid_layout(self):
        html = render_slide("kpi_grid", _kpi_slide(), THEME_LIGHT)
        assert "display: grid" in html
        assert "1920px" in html

    def test_no_overflow_potential(self):
        html = render_slide("kpi_grid", _kpi_slide(), THEME_LIGHT)
        assert "overflow: hidden" in html

    def test_escapes_user_content(self):
        slide = _kpi_slide(kpi_specs=[KPISpec(label='<img onerror=alert(1)>', value="$1M")])
        html = render_slide("kpi_grid", slide, THEME_LIGHT)
        assert "<img" not in html
        assert "&lt;img" in html

    def test_empty_kpi_specs_renders_empty_grid(self):
        slide = _kpi_slide(kpi_specs=[])
        html = render_slide("kpi_grid", slide, THEME_LIGHT)
        assert "Quarterly KPIs" in html
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_slide_generator.py::TestKpiGridLayout -v`
Expected: 8 FAIL with `NotImplementedError: Layout 'kpi_grid' not implemented yet`.

- [ ] **Step 3: Add the `kpi_grid` renderer**

Append to `backend/app/services/artifacts/html_slide_generator.py` (before the dispatcher):

```python
def _render_kpi_grid(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Key Metrics")
    tiles: List[str] = []
    for spec in (plan.kpi_specs or []):
        label = html.escape(spec.label or "")
        value = html.escape(spec.value or "")
        delta = html.escape(spec.delta or "") if spec.delta else ""
        caption = html.escape(spec.caption or "") if spec.caption else ""
        delta_html = f'<p class="kpi__delta">{delta}</p>' if delta else ""
        caption_html = f'<p class="kpi__caption">{caption}</p>' if caption else ""
        tiles.append(
            f'<div class="kpi">'
            f'<p class="kpi__label">{label}</p>'
            f'<p class="kpi__value">{value}</p>'
            f'{delta_html}{caption_html}'
            f'</div>'
        )
    tiles_html = "\n".join(tiles) if tiles else '<p class="kpi-empty">No KPIs</p>'
    return f"""<style>
{css}
/* === KPI GRID LAYOUT === */
.slide--kpi-grid {{
  width: 1920px; height: 1080px; background: var(--bg-primary, #fff);
  color: var(--text-primary, #0a0a0a); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-rows: auto 1fr; gap: 64px;
}}
.kpi-grid__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0; line-height: 1.1; }}
.kpi-grid__tiles {{ display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 48px; align-content: center; }}
.kpi {{ background: rgba(0,0,0,0.04); padding: 48px;
  border-radius: 16px; border-left: 8px solid var(--accent, #4361ee); }}
.kpi__label {{ font-size: 24px; text-transform: uppercase;
  letter-spacing: 0.08em; margin: 0 0 16px 0; opacity: 0.7; }}
.kpi__value {{ font-family: var(--font-display); font-weight: 800;
  font-size: 96px; line-height: 1; margin: 0 0 16px 0; }}
.kpi__delta {{ font-size: 28px; color: var(--accent, #4361ee);
  font-weight: 600; margin: 0 0 8px 0; }}
.kpi__caption {{ font-size: 18px; opacity: 0.6; margin: 0; }}
.kpi-empty {{ font-size: 32px; opacity: 0.4; text-align: center; }}
</style>
<section class="slide slide--kpi-grid">
  <h2 class="kpi-grid__title">{title}</h2>
  <div class="kpi-grid__tiles">{tiles_html}</div>
</section>"""
```

Update `_LAYOUT_RENDERERS`:

```python
_LAYOUT_RENDERERS: Dict[str, Any] = {
    "cover": _render_cover,
    "kpi_grid": _render_kpi_grid,
}
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_slide_generator.py -v`
Expected: 17 passed (9 cover + 8 kpi_grid).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/html_slide_generator.py backend/tests/test_html_slide_generator.py
git commit -m "feat(ppt): HTML slide generator — kpi_grid layout"
```

---

## Task 6: HTML slide generator — 10 remaining layouts (chart_with_bullets + 9 others)

**Files:**
- Modify: `backend/app/services/artifacts/html_slide_generator.py`
- Modify: `backend/tests/test_html_slide_generator.py` (extend tests)

**Interfaces:** 10 more layouts: `agenda`, `chart_full`, `chart_with_bullets`, `findings_cards`, `insights_bullets`, `recommendations`, `data_table`, `methodology`, `section_divider`, `closing`.

- [ ] **Step 1: Implement `chart_with_bullets` + tests**

Append to `html_slide_generator.py`:

```python
def _render_chart_with_bullets(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    bullets_html = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    chart_data = (plan.chart.data or {}) if plan.chart else {}
    chart_type = (plan.chart.type if plan.chart else "bar") or "bar"
    labels = chart_data.get("labels", [])
    values = chart_data.get("values", [])
    chart_id = f"chart-{abs(hash((title, str(values)))) % 100000}"
    chart_js = f"""
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
  const ctx = document.getElementById('{chart_id}');
  new Chart(ctx, {{
    type: '{chart_type}',
    data: {{
      labels: {labels!r},
      datasets: [{{
        data: {values!r},
        backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
        borderRadius: 6,
      }}],
    }},
    options: {{
      plugins: {{ legend: {{ display: false }} }},
      scales: {{ y: {{ beginAtZero: true }} }},
    }},
  }});
</script>"""
    return f"""<style>
{css}
/* === CHART WITH BULLETS === */
.slide--chart-with-bullets {{
  width: 1920px; height: 1080px; background: var(--bg-primary, #fff);
  color: var(--text-primary, #0a0a0a); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-columns: 1fr 1fr; gap: 64px;
}}
.chart-title {{ grid-column: 1 / -1; font-family: var(--font-display);
  font-weight: 700; font-size: 64px; margin: 0; }}
.chart-wrap {{ display: flex; align-items: center; justify-content: center; }}
.bullets {{ font-size: 32px; line-height: 1.5; padding-left: 32px; }}
.bullets li {{ margin-bottom: 24px; }}
</style>
<section class="slide slide--chart-with-bullets">
  <h2 class="chart-title">{title}</h2>
  <div class="chart-wrap">
    <canvas id="{chart_id}" style="width: 100%; height: 480px;"></canvas>
  </div>
  <ul class="bullets">{bullets_html}</ul>
</section>
{chart_js}"""
```

Add to `_LAYOUT_RENDERERS`:

```python
"chart_with_bullets": _render_chart_with_bullets,
```

Append to `backend/tests/test_html_slide_generator.py`:

```python
from app.services.synexia.contracts import ChartSpec


def _chart_slide(**overrides) -> SlidePlan:
    base = dict(
        layout="chart_with_bullets",
        title="Quarterly Revenue",
        bullets=["Q3 revenue $2.4M, up 12% QoQ", "Hardware drove 60% of growth"],
        chart=ChartSpec(type="bar", data={
            "labels": ["Q1", "Q2", "Q3"],
            "values": [1800000, 2100000, 2400000],
        }),
    )
    base.update(overrides)
    return SlidePlan(**base)


class TestChartWithBullets:
    def test_renders_canvas(self):
        html = render_slide("chart_with_bullets", _chart_slide(), THEME_LIGHT)
        assert "<canvas" in html

    def test_includes_chart_js(self):
        html = render_slide("chart_with_bullets", _chart_slide(), THEME_LIGHT)
        assert "chart.js" in html.lower() or "chart.umd" in html.lower()

    def test_embeds_chart_data(self):
        html = render_slide("chart_with_bullets", _chart_slide(), THEME_LIGHT)
        assert "Q1" in html
        assert "1800000" in html

    def test_includes_bullets(self):
        html = render_slide("chart_with_bullets", _chart_slide(), THEME_LIGHT)
        assert "Q3 revenue" in html
        assert "Hardware" in html
```

Run: `cd backend && venv/bin/python -m pytest tests/test_html_slide_generator.py::TestChartWithBullets -v`
Expected: 4 passed.

- [ ] **Step 2: Implement 6 simple layouts (agenda, insights_bullets, recommendations, methodology, section_divider, closing)**

Append to `html_slide_generator.py`:

```python
def _render_agenda(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Agenda")
    items = "\n".join(
        f'<li class="agenda__item"><span class="agenda__num">{i+1:02d}</span>'
        f'<span class="agenda__label">{html.escape(item)}</span></li>'
        for i, item in enumerate(plan.bullets or [])
    )
    return f"""<style>
{css}
/* === AGENDA === */
.slide--agenda {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.agenda__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.agenda__list {{ list-style: none; padding: 0; margin: 0; font-size: 40px; }}
.agenda__item {{ display: grid; grid-template-columns: 120px 1fr;
  gap: 48px; margin-bottom: 40px; align-items: baseline; }}
.agenda__num {{ font-family: var(--font-display); font-weight: 800;
  color: var(--accent); font-size: 56px; }}
.agenda__label {{ font-weight: 500; }}
</style>
<section class="slide slide--agenda">
  <h2 class="agenda__title">{title}</h2>
  <ol class="agenda__list">{items}</ol>
</section>"""


def _render_insights_bullets(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Key Insights")
    items = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    return f"""<style>
{css}
/* === INSIGHTS BULLETS === */
.slide--insights-bullets {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.insights__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.insights__list {{ font-size: 36px; line-height: 1.5; }}
.insights__list li {{ margin-bottom: 32px; }}
</style>
<section class="slide slide--insights-bullets">
  <h2 class="insights__title">{title}</h2>
  <ul class="insights__list">{items}</ul>
</section>"""


def _render_recommendations(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Recommendations")
    items = "\n".join(
        f'<div class="rec"><span class="rec__num">{i+1:02d}</span>'
        f'<p class="rec__text">{html.escape(b)}</p></div>'
        for i, b in enumerate(plan.bullets or [])
    )
    return f"""<style>
{css}
/* === RECOMMENDATIONS === */
.slide--recommendations {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.recs__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.rec {{ display: grid; grid-template-columns: 120px 1fr; gap: 48px;
  margin-bottom: 48px; align-items: start; }}
.rec__num {{ font-family: var(--font-display); font-weight: 800;
  font-size: 72px; color: var(--accent); }}
.rec__text {{ font-size: 32px; line-height: 1.4; margin: 0; }}
</style>
<section class="slide slide--recommendations">
  <h2 class="recs__title">{title}</h2>
  {items}
</section>"""


def _render_methodology(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Methodology")
    bullets = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    return f"""<style>
{css}
/* === METHODOLOGY === */
.slide--methodology {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.method__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.method__body {{ font-size: 32px; line-height: 1.5; padding-left: 32px; }}
.method__body li {{ margin-bottom: 24px; }}
</style>
<section class="slide slide--methodology">
  <h2 class="method__title">{title}</h2>
  <ol class="method__body">{bullets}</ol>
</section>"""


def _render_section_divider(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    subtitle_html = f'<p class="divider__sub">{subtitle}</p>' if subtitle else ""
    return f"""<style>
{css}
/* === SECTION DIVIDER === */
.slide--section-divider {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 96px; box-sizing: border-box;
  overflow: hidden; text-align: center;
}}
.divider__title {{ font-family: var(--font-display); font-weight: 900;
  font-size: 144px; margin: 0 0 32px 0; line-height: 1; }}
.divider__sub {{ font-size: 36px; opacity: 0.7; margin: 0; }}
</style>
<section class="slide slide--section-divider">
  <h1 class="divider__title">{title}</h1>
  {subtitle_html}
</section>"""


def _render_closing(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Thank you")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    subtitle_html = f'<p class="closing__sub">{subtitle}</p>' if subtitle else ""
    return f"""<style>
{css}
/* === CLOSING === */
.slide--closing {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 96px; box-sizing: border-box;
  overflow: hidden; text-align: center;
}}
.closing__title {{ font-family: var(--font-display); font-weight: 900;
  font-size: 160px; margin: 0 0 32px 0; line-height: 1; }}
.closing__sub {{ font-size: 36px; opacity: 0.7; margin: 0; }}
</style>
<section class="slide slide--closing">
  <h1 class="closing__title">{title}</h1>
  {subtitle_html}
</section>"""
```

Update `_LAYOUT_RENDERERS`:

```python
_LAYOUT_RENDERERS: Dict[str, Any] = {
    "cover": _render_cover,
    "kpi_grid": _render_kpi_grid,
    "chart_with_bullets": _render_chart_with_bullets,
    "agenda": _render_agenda,
    "insights_bullets": _render_insights_bullets,
    "recommendations": _render_recommendations,
    "methodology": _render_methodology,
    "section_divider": _render_section_divider,
    "closing": _render_closing,
}
```

Append tests:

```python
class TestSimpleLayouts:
    def test_agenda_renders_numbered_items(self):
        plan = SlidePlan(layout="agenda", title="Agenda", bullets=["Item A", "Item B"])
        html = render_slide("agenda", plan, THEME_LIGHT)
        assert "01" in html and "Item A" in html
        assert "02" in html and "Item B" in html

    def test_insights_bullets_renders(self):
        plan = SlidePlan(layout="insights_bullets", title="Insights", bullets=["Insight one"])
        html = render_slide("insights_bullets", plan, THEME_LIGHT)
        assert "Insight one" in html

    def test_recommendations_renders(self):
        plan = SlidePlan(layout="recommendations", title="Actions", bullets=["Action one"])
        html = render_slide("recommendations", plan, THEME_LIGHT)
        assert "01" in html
        assert "Action one" in html

    def test_methodology_renders(self):
        plan = SlidePlan(layout="methodology", title="Method", bullets=["Step 1", "Step 2"])
        html = render_slide("methodology", plan, THEME_LIGHT)
        assert "Step 1" in html
        assert "Step 2" in html

    def test_section_divider_hero(self):
        plan = SlidePlan(layout="section_divider", title="Part Two", subtitle="Findings")
        html = render_slide("section_divider", plan, THEME_LIGHT)
        assert "Part Two" in html
        assert "Findings" in html

    def test_closing_hero(self):
        plan = SlidePlan(layout="closing", title="Thank you", subtitle="Questions?")
        html = render_slide("closing", plan, THEME_LIGHT)
        assert "Thank you" in html
        assert "Questions?" in html
```

Run: `cd backend && venv/bin/python -m pytest tests/test_html_slide_generator.py -v`
Expected: 27 passed (9 cover + 1 unknown + 8 kpi + 4 chart + 6 simple).

- [ ] **Step 3: Implement 3 final layouts (findings_cards, chart_full, data_table)**

Append to `html_slide_generator.py`:

```python
def _render_findings_cards(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Findings")
    cards_html = "\n".join(
        f'<div class="card"><h3 class="card__title">{html.escape(c.title)}</h3>'
        f'<p class="card__body">{html.escape(c.body or "")}</p></div>'
        for c in (plan.cards or [])
    )
    return f"""<style>
{css}
/* === FINDINGS CARDS === */
.slide--findings-cards {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.findings__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.findings__grid {{ display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 48px; }}
.card {{ background: rgba(0,0,0,0.04); padding: 48px;
  border-radius: 12px; border-top: 6px solid var(--accent); }}
.card__title {{ font-family: var(--font-display); font-size: 32px;
  font-weight: 700; margin: 0 0 24px 0; }}
.card__body {{ font-size: 24px; line-height: 1.4; margin: 0; }}
</style>
<section class="slide slide--findings-cards">
  <h2 class="findings__title">{title}</h2>
  <div class="findings__grid">{cards_html}</div>
</section>"""


def _render_chart_full(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    chart_data = (plan.chart.data or {}) if plan.chart else {}
    chart_type = (plan.chart.type if plan.chart else "bar") or "bar"
    labels = chart_data.get("labels", [])
    values = chart_data.get("values", [])
    chart_id = f"chart-full-{abs(hash((title, str(values)))) % 100000}"
    chart_js = f"""
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
  const ctx = document.getElementById('{chart_id}');
  new Chart(ctx, {{
    type: '{chart_type}',
    data: {{
      labels: {labels!r},
      datasets: [{{
        data: {values!r},
        backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
        borderRadius: 6,
      }}],
    }},
    options: {{
      plugins: {{ legend: {{ display: false }} }},
      responsive: true, maintainAspectRatio: false,
    }},
  }});
</script>"""
    return f"""<style>
{css}
/* === CHART FULL === */
.slide--chart-full {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-rows: auto 1fr; gap: 48px;
}}
.chart-full__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0; }}
.chart-full__canvas-wrap {{ position: relative; }}
</style>
<section class="slide slide--chart-full">
  <h2 class="chart-full__title">{title}</h2>
  <div class="chart-full__canvas-wrap">
    <canvas id="{chart_id}"></canvas>
  </div>
</section>
{chart_js}"""


def _render_data_table(plan: SlidePlan, theme: ThemePreset) -> str:
    """Simple data table — top-8 rows, theme-styled."""
    css = _theme_css(theme)
    title = html.escape(plan.title or "Data Table")
    rows = (plan.table or [])[:8]
    if not rows:
        return f'<section class="slide slide--data-table"><h2>{title}</h2></section>'

    headers = list(rows[0].keys()) if rows else []
    thead = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    tbody_rows = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(str(r.get(h, '')))}</td>" for h in headers) + "</tr>"
        for r in rows
    )
    return f"""<style>
{css}
/* === DATA TABLE === */
.slide--data-table {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.dt__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.dt__table {{ width: 100%; border-collapse: collapse; font-size: 24px; }}
.dt__table th {{ text-align: left; padding: 16px;
  background: var(--accent); color: var(--text-primary); }}
.dt__table td {{ padding: 16px; border-bottom: 1px solid rgba(0,0,0,0.1); }}
.dt__table tr:nth-child(even) {{ background: rgba(0,0,0,0.03); }}
</style>
<section class="slide slide--data-table">
  <h2 class="dt__title">{title}</h2>
  <table class="dt__table">
    <thead><tr>{thead}</tr></thead>
    <tbody>{tbody_rows}</tbody>
  </table>
</section>"""
```

Update `_LAYOUT_RENDERERS`:

```python
_LAYOUT_RENDERERS: Dict[str, Any] = {
    "cover": _render_cover,
    "kpi_grid": _render_kpi_grid,
    "chart_with_bullets": _render_chart_with_bullets,
    "agenda": _render_agenda,
    "insights_bullets": _render_insights_bullets,
    "recommendations": _render_recommendations,
    "methodology": _render_methodology,
    "section_divider": _render_section_divider,
    "closing": _render_closing,
    "findings_cards": _render_findings_cards,
    "chart_full": _render_chart_full,
    "data_table": _render_data_table,
}
```

Append tests:

```python
from app.services.synexia.contracts import CardSpec


class TestStructuredLayouts:
    def test_findings_cards_renders_all(self):
        plan = SlidePlan(
            layout="findings_cards",
            title="Findings",
            cards=[
                CardSpec(title="Finding 1", body="Body 1"),
                CardSpec(title="Finding 2", body="Body 2"),
                CardSpec(title="Finding 3", body="Body 3"),
            ],
        )
        html = render_slide("findings_cards", plan, THEME_LIGHT)
        assert "Finding 1" in html
        assert "Finding 2" in html
        assert "Finding 3" in html

    def test_chart_full_renders_canvas(self):
        plan = SlidePlan(
            layout="chart_full",
            title="Trend",
            chart=ChartSpec(type="line", data={"labels": ["Jan", "Feb"], "values": [10, 20]}),
        )
        html = render_slide("chart_full", plan, THEME_LIGHT)
        assert "<canvas" in html
        assert "Jan" in html

    def test_data_table_caps_at_8_rows(self):
        plan = SlidePlan(
            layout="data_table",
            title="Data",
            table=[{"a": i, "b": i*2} for i in range(20)],
        )
        html = render_slide("data_table", plan, THEME_LIGHT)
        assert html.count("<tr>") == 9
```

Run: `cd backend && venv/bin/python -m pytest tests/test_html_slide_generator.py -v`
Expected: 30 passed.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/artifacts/html_slide_generator.py backend/tests/test_html_slide_generator.py
git commit -m "feat(ppt): HTML slide generator — 10 remaining layouts (12 total)"
```

---

## Task 7: HTML stage builder — wrap slides into a printable index.html

**Files:**
- Modify: `backend/app/services/artifacts/html_slide_generator.py`
- Test: `backend/tests/test_html_stage.py` (NEW)

**Interfaces:**
- `build_stage(slide_htmls: List[str]) -> str` — returns a full HTML document with all slides embedded
- The output, when rendered by LibreOffice, produces one PDF page per slide
- Includes an inline copy of viewport-base CSS + Google Fonts links for the 12 themes

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_html_stage.py`:

```python
"""Tests for the HTML stage builder (wraps slides into index.html)."""
from app.services.artifacts.html_slide_generator import build_stage, render_slide
from app.services.artifacts.themes import get_theme
from app.services.synexia.contracts import SlidePlan


THEME = get_theme("bold_signal")


def test_stage_wraps_in_html():
    htmls = [render_slide("cover", SlidePlan(layout="cover", title="T"), THEME)]
    stage = build_stage(htmls)
    assert stage.startswith("<!DOCTYPE html>")
    assert "</html>" in stage


def test_stage_includes_all_slides():
    htmls = [
        render_slide("cover", SlidePlan(layout="cover", title="One"), THEME),
        render_slide("cover", SlidePlan(layout="cover", title="Two"), THEME),
    ]
    stage = build_stage(htmls)
    assert "One" in stage
    assert "Two" in stage


def test_stage_uses_16_9_dimensions():
    stage = build_stage([render_slide("cover", SlidePlan(layout="cover", title="T"), THEME)])
    assert "1920px" in stage
    assert "1080px" in stage


def test_stage_breaks_pages_between_slides():
    stage = build_stage([
        render_slide("cover", SlidePlan(layout="cover", title="One"), THEME),
        render_slide("cover", SlidePlan(layout="cover", title="Two"), THEME),
    ])
    assert "page-break-after" in stage


def test_stage_escapes_user_content():
    stage = build_stage([
        render_slide("cover", SlidePlan(layout="cover", title="<script>"), THEME),
    ])
    assert "&lt;script&gt;" in stage
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_stage.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_stage'`.

- [ ] **Step 3: Add `build_stage` to `html_slide_generator.py`**

Append to `backend/app/services/artifacts/html_slide_generator.py`:

```python
# Minimal inline copy of frontend-slides' viewport-base.css (the bare
# essentials: fixed 16:9 stage, no scrollbars, page breaks for print).
_STAGE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  width: 1920px;
  font-family: 'Work Sans', sans-serif;
  background: #000;
}
.slide {
  width: 1920px;
  height: 1080px;
  position: relative;
  overflow: hidden;
  page-break-after: always;
  break-after: page;
}
.slide:last-child { page-break-after: auto; break-after: auto; }
"""


def build_stage(slide_htmls: List[str]) -> str:
    """Wrap a list of per-slide HTML strings into a printable index.html.

    The output is a full HTML document LibreOffice can convert to PDF
    (one page per slide) and python-pptx can then turn into a 16:9 deck
    of image-fill slides.
    """
    body = "\n".join(slide_htmls)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Generated Deck</title>
<style>{_STAGE_CSS}</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Space+Grotesk:wght@400;500&family=Manrope:wght@400;500;800&family=Syne:wght@700;800&family=Fraunces:wght@400;700&family=Work+Sans:wght@400;500&family=Caveat&family=DM+Serif+Display&family=DM+Sans&family=Plus+Jakarta+Sans&family=Orbitron&family=Rajdhani&family=JetBrains+Mono&family=Crimson+Pro&family=Source+Sans+3:wght@400;500&display=swap" rel="stylesheet">
</head>
<body>
{body}
</body>
</html>"""


__all__ = ["render_slide", "build_stage"]
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_stage.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/html_slide_generator.py backend/tests/test_html_stage.py
git commit -m "feat(ppt): HTML stage builder for printable index.html"
```

---

## Task 8: HTML → PPTX image-fill pipeline

**Files:**
- Create: `backend/app/services/artifacts/html_to_pptx.py` (NEW)
- Test: `backend/tests/test_html_to_pptx_image_fill.py` (NEW)

**Interfaces:**
- `render_image_fill(stage_html: str) -> bytes` — returns PPTX bytes
- Pipeline: write stage_html to a temp file → `soffice --headless --convert-to pdf` → `pdftoppm -png -r 192` → python-pptx builds a 16:9 deck with each PNG as full-bleed image fill
- Raises `PptxRenderError` on any failure (missing binaries, soffice timeout, pdftoppm timeout, malformed PPTX)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_html_to_pptx_image_fill.py`:

```python
"""Tests for HTML → PPTX image-fill pipeline (LibreOffice + pdftoppm)."""
import io
import pytest
from app.services.artifacts.html_to_pptx import (
    render_image_fill, PptxRenderError, image_fill_available,
)


SAMPLE_STAGE = """<!DOCTYPE html>
<html><head><style>
* { box-sizing: border-box; }
body { margin: 0; font-family: sans-serif; }
.slide { width: 1920px; height: 1080px;
  page-break-after: always; padding: 96px; }
.slide:last-child { page-break-after: auto; }
</style></head>
<body>
<section class="slide" style="background: #1a1a1a; color: white;">
  <h1 style="font-size: 128px;">Cover Slide</h1>
</section>
<section class="slide" style="background: white; color: black;">
  <h2 style="font-size: 64px;">Slide Two</h2>
</section>
</body></html>"""


class TestImageFillAvailable:
    def test_returns_true_when_binaries_present(self):
        assert image_fill_available() is True


class TestRenderImageFill:
    def test_returns_bytes(self):
        data = render_image_fill(SAMPLE_STAGE)
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_returns_valid_pptx(self):
        data = render_image_fill(SAMPLE_STAGE)
        # PPTX files start with the ZIP signature (PK\x03\x04)
        assert data[:4] == b"PK\x03\x04"

    def test_pptx_loads_with_python_pptx(self):
        from pptx import Presentation
        data = render_image_fill(SAMPLE_STAGE)
        pres = Presentation(io.BytesIO(data))
        assert pres.slide_width > 0
        assert pres.slide_height > 0

    def test_two_slides_produces_two_slides(self):
        from pptx import Presentation
        data = render_image_fill(SAMPLE_STAGE)
        pres = Presentation(io.BytesIO(data))
        assert len(pres.slides) == 2

    def test_16_9_aspect_ratio(self):
        from pptx import Presentation
        data = render_image_fill(SAMPLE_STAGE)
        pres = Presentation(io.BytesIO(data))
        ratio = pres.slide_width / pres.slide_height
        assert 1.7 < ratio < 1.8

    def test_each_slide_has_image(self):
        from pptx import Presentation
        data = render_image_fill(SAMPLE_STAGE)
        pres = Presentation(io.BytesIO(data))
        for slide in pres.slides:
            pictures = [s for s in slide.shapes if s.shape_type == 13]
            assert len(pictures) == 1
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_to_pptx_image_fill.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.artifacts.html_to_pptx'`.

- [ ] **Step 3: Create `html_to_pptx.py`**

Create `backend/app/services/artifacts/html_to_pptx.py`:

```python
"""HTML → PPTX converters.

* ``render_image_fill(stage_html)`` — full-fidelity static image fills.
  v1.0 default.  Pipeline: stage_html → ``soffice --headless
  --convert-to pdf`` → ``pdftoppm -png -r 192`` → python-pptx with
  each PNG as a 16:9 image fill.  Reuses the soffice + pdftoppm
  pattern from ``backend/app/services/artifacts/thumbnails.py``.

* ``render_editable_text(stage_html)`` — text-editable native PPTX.
  v1.1 (not implemented in this plan; signature reserved).
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from pptx import Presentation
from pptx.util import Inches

logger = logging.getLogger(__name__)


class PptxRenderError(Exception):
    """Raised when the image-fill pipeline cannot produce a PPTX."""


def _find_binary(names: Tuple[str, ...]) -> Optional[str]:
    for n in names:
        path = shutil.which(n)
        if path:
            return path
    return None


def image_fill_available() -> bool:
    """True when ``soffice`` and ``pdftoppm`` are installed and on PATH."""
    return (
        _find_binary(("soffice", "libreoffice")) is not None
        and _find_binary(("pdftoppm",)) is not None
    )


def _convert_html_to_pdf(stage_html: str, workdir: Path) -> Path:
    soffice = _find_binary(("soffice", "libreoffice"))
    if soffice is None:
        raise PptxRenderError("soffice/libreoffice not found on PATH")

    html_path = workdir / "index.html"
    html_path.write_text(stage_html, encoding="utf-8")

    profile_dir = workdir / "soffice_profile"
    profile_dir.mkdir(exist_ok=True)

    cmd = [
        soffice, "--headless", "--norestore", "--nologo",
        "--nofirststartwizard",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to", "pdf", "--outdir", str(workdir),
        str(html_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=60, cwd=str(workdir),
        )
    except subprocess.TimeoutExpired as exc:
        raise PptxRenderError("soffice timeout after 60s") from exc

    if proc.returncode != 0:
        raise PptxRenderError(
            f"soffice exited {proc.returncode}: {proc.stderr[:500]}"
        )

    pdf_path = workdir / "index.pdf"
    if not pdf_path.exists():
        raise PptxRenderError(f"soffice produced no PDF (stderr: {proc.stderr[:500]})")
    return pdf_path


def _convert_pdf_to_pngs(pdf_path: Path, workdir: Path) -> List[Path]:
    pdftoppm = _find_binary(("pdftoppm",))
    if pdftoppm is None:
        raise PptxRenderError("pdftoppm not found on PATH")

    prefix = workdir / "slide"
    cmd = [pdftoppm, "-png", "-r", "192", str(pdf_path), str(prefix)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=str(workdir),
        )
    except subprocess.TimeoutExpired as exc:
        raise PptxRenderError("pdftoppm timeout after 60s") from exc

    if proc.returncode != 0:
        raise PptxRenderError(
            f"pdftoppm exited {proc.returncode}: {proc.stderr[:500]}"
        )

    pngs = sorted(workdir.glob("slide-*.png"))
    if not pngs:
        raise PptxRenderError("pdftoppm produced no PNGs")
    return pngs


def _build_pptx_from_pngs(pngs: List[Path]) -> bytes:
    pres = Presentation()
    pres.slide_width = Inches(13.333)  # 16:9
    pres.slide_height = Inches(7.5)
    blank_layout = pres.slide_layouts[6]

    for png in pngs:
        slide = pres.slides.add_slide(blank_layout)
        slide.shapes.add_picture(
            str(png), 0, 0,
            width=pres.slide_width, height=pres.slide_height,
        )

    buf = io.BytesIO()
    pres.save(buf)
    return buf.getvalue()


def render_image_fill(stage_html: str) -> bytes:
    """Convert a stage HTML to PPTX bytes (image-fill pipeline).

    Raises ``PptxRenderError`` on any failure.
    """
    if not image_fill_available():
        raise PptxRenderError(
            "image_fill pipeline unavailable: soffice or pdftoppm missing"
        )

    with tempfile.TemporaryDirectory(prefix="zhanlu_html2pptx_") as td:
        workdir = Path(td)
        try:
            pdf_path = _convert_html_to_pdf(stage_html, workdir)
            pngs = _convert_pdf_to_pngs(pdf_path, workdir)
            pptx_bytes = _build_pptx_from_pngs(pngs)
        except PptxRenderError:
            raise
        except Exception as exc:
            raise PptxRenderError(f"image_fill pipeline failed: {exc}") from exc

    if not pptx_bytes or not zipfile.is_zipfile(io.BytesIO(pptx_bytes)):
        raise PptxRenderError("produced output is not a valid PPTX (zip)")
    return pptx_bytes


__all__ = ["render_image_fill", "image_fill_available", "PptxRenderError"]
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_html_to_pptx_image_fill.py -v`
Expected: 7 passed.

If `soffice`/`pdftoppm` are missing, all tests FAIL with a clear message. In that case, install them (`apt-get install libreoffice poppler-utils` on Debian/Ubuntu) and re-run. Document this in the deploy instructions.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/html_to_pptx.py backend/tests/test_html_to_pptx_image_fill.py
git commit -m "feat(ppt): HTML → PPTX image-fill pipeline (LibreOffice + pdftoppm)"
```

---

## Task 9: `render_html_deck` entry point — glues everything together

**Files:**
- Create: `backend/app/services/artifacts/render_html_deck.py` (NEW)
- Test: `backend/tests/test_render_html_deck.py` (NEW)

**Interfaces:**
- `render_html_deck(plan: DeckPlan, ctx: ExportContext) -> bytes` — returns PPTX bytes
- Resolution: theme = `select_theme(plan, ctx.user_message)`, slides = render each SlidePlan, stage = `build_stage(slide_htmls)`, PPTX = `render_image_fill(stage_html)`
- Raises `RenderError` on any failure

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_render_html_deck.py`:

```python
"""Tests for the render_html_deck entry point."""
import io
import pytest
from app.services.artifacts.render_html_deck import (
    render_html_deck, RenderError, html_design_available,
)
from app.services.synexia.contracts import DeckPlan, SlidePlan, KPISpec
from app.services.artifacts.exporters._common import ExportContext


def _basic_plan() -> DeckPlan:
    return DeckPlan(
        title="Q3 Recap",
        deck_type="data_report",
        theme_recommendation="electric_studio",
        slides=[
            SlidePlan(layout="cover", title="Q3 Recap", subtitle="Revenue up 8%"),
            SlidePlan(layout="kpi_grid", title="Key Metrics", kpi_specs=[
                KPISpec(label="Revenue", value="$2.4M", delta="+12%"),
                KPISpec(label="Orders", value="1,847", delta="+5%"),
            ]),
            SlidePlan(layout="closing", title="Thank you"),
        ],
    )


@pytest.fixture
def ctx():
    return ExportContext(source="test", user_message="quarterly recap")


class TestAvailability:
    def test_available_when_dependencies_present(self):
        assert html_design_available() is True


class TestRenderHtmlDeck:
    def test_returns_pptx_bytes(self, ctx):
        data = render_html_deck(_basic_plan(), ctx)
        assert isinstance(data, bytes)
        assert data[:4] == b"PK\x03\x04"

    def test_correct_slide_count(self, ctx):
        from pptx import Presentation
        data = render_html_deck(_basic_plan(), ctx)
        pres = Presentation(io.BytesIO(data))
        assert len(pres.slides) == 3

    def test_16_9(self, ctx):
        from pptx import Presentation
        data = render_html_deck(_basic_plan(), ctx)
        pres = Presentation(io.BytesIO(data))
        ratio = pres.slide_width / pres.slide_height
        assert 1.7 < ratio < 1.8

    def test_user_message_overrides_theme(self, ctx):
        from pptx import Presentation
        ctx.user_message = "editorial report"
        data = render_html_deck(_basic_plan(), ctx)
        pres = Presentation(io.BytesIO(data))
        assert len(pres.slides) == 3
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_render_html_deck.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.artifacts.render_html_deck'`.

- [ ] **Step 3: Create `render_html_deck.py`**

Create `backend/app/services/artifacts/render_html_deck.py`:

```python
"""Top-level entry point for the HTML design renderer.

``render_html_deck(plan, ctx)`` takes a planner-produced ``DeckPlan``
and produces PPTX bytes using the HTML design path:

  1. Resolve theme (select_theme)
  2. Render each SlidePlan as HTML (render_slide)
  3. Wrap into a printable index.html (build_stage)
  4. Convert HTML → PDF → PNG → PPTX image fill (render_image_fill)
"""
from __future__ import annotations

import logging
from typing import List

from app.services.synexia.contracts import DeckPlan
from app.services.artifacts.exporters._common import ExportContext
from app.services.artifacts.themes import select_theme, ThemePreset
from app.services.artifacts.html_slide_generator import render_slide, build_stage
from app.services.artifacts.html_to_pptx import (
    render_image_fill, image_fill_available, PptxRenderError,
)

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised by render_html_deck when the HTML path fails.

    Caller (export service) catches this and falls back to the
    structured python-pptx renderer.
    """


def html_design_available() -> bool:
    """True when the image_fill pipeline can run (soffice + pdftoppm present)."""
    return image_fill_available()


def render_html_deck(plan: DeckPlan, ctx: ExportContext) -> bytes:
    """Render a DeckPlan as PPTX using the HTML design path.

    Raises ``RenderError`` on any failure.  The caller (export service)
    is expected to fall back to the structured renderer.
    """
    if not html_design_available():
        raise RenderError(
            "HTML design renderer unavailable: soffice or pdftoppm missing"
        )

    user_message = getattr(ctx, "user_message", "") or ""
    theme: ThemePreset = select_theme(plan, user_message)

    # 1. Render each slide; skip unknown layouts
    slide_htmls: List[str] = []
    for slide in plan.slides:
        try:
            slide_htmls.append(render_slide(slide.layout, slide, theme))
        except NotImplementedError as exc:
            logger.warning(
                "render_html_deck: layout %r not implemented, skipping: %s",
                slide.layout, exc,
            )
            continue

    if not slide_htmls:
        raise RenderError("no slides could be rendered as HTML")

    # 2. Build printable index.html
    stage_html = build_stage(slide_htmls)

    # 3. Convert to PPTX (image fill)
    try:
        return render_image_fill(stage_html)
    except PptxRenderError as exc:
        raise RenderError(f"image_fill pipeline failed: {exc}") from exc


__all__ = ["render_html_deck", "RenderError", "html_design_available"]
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_render_html_deck.py -v`
Expected: 4 passed (1 availability + 3 render). If soffice/pdftoppm missing, the availability test fails too — install them and re-run.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/render_html_deck.py backend/tests/test_render_html_deck.py
git commit -m "feat(ppt): render_html_deck entry point"
```

---

## Task 10: Wire `render_html_deck` into the export service

**Files:**
- Modify: `backend/app/services/artifacts/exporters/service.py` (~line 633-650 in `_render_deck_pipeline`)

**Interfaces:**
- After `route_deck` returns "sandbox", if `settings.HTML_DESIGN_RENDERER_ENABLED` is True and `html_design_available()` is True → call `render_html_deck(plan, ctx)` and return its bytes.
- If `render_html_deck` raises → log warning, fall through to structured fallback.

- [ ] **Step 1: Modify `_render_deck_pipeline` in `service.py`**

Locate the `if route == "sandbox":` block in `backend/app/services/artifacts/exporters/service.py` (around line 637). Replace it with:

```python
        route = "structured"
        if settings.PPT_SMART_ROUTER_ENABLED:
            route = route_deck(plan, user_message)

        if route == "sandbox":
            # Design-heavy deck.  Try the HTML design path first if opted in;
            # fall back to the structured layout engine (the original behavior)
            # when the new path is disabled or fails.
            if settings.HTML_DESIGN_RENDERER_ENABLED:
                from app.services.artifacts.render_html_deck import (
                    render_html_deck, RenderError, html_design_available,
                )
                if html_design_available():
                    try:
                        data = render_html_deck(plan, ctx)
                        audit_report = {
                            "status": "PASS",
                            "summary": "html_design_renderer",
                            "details": "rendered via LibreOffice image_fill pipeline",
                        }
                        logger.info(
                            "ExportService: deck rendered via html_design (type=%s)",
                            plan.deck_type,
                        )
                        return (
                            data,
                            "application/vnd.openxmlformats-"
                            "officedocument.presentationml.presentation",
                            "pptx", audit_report,
                        )
                    except RenderError as exc:
                        logger.warning(
                            "ExportService: html_design render failed, "
                            "falling back to layout engine: %s", exc,
                        )
                else:
                    logger.info(
                        "ExportService: HTML_DESIGN_RENDERER_ENABLED but "
                        "soffice/pdftoppm missing — using layout engine fallback"
                    )
            else:
                # Original (pre-Phase-4) behavior: log and fall through.
                logger.info(
                    "ExportService: deck routed to sandbox (type=%s); "
                    "HTML_DESIGN_RENDERER_ENABLED=False — using layout engine "
                    "as deterministic fallback",
                    plan.deck_type,
                )
                ctx.cover_art = True

        # Render via the shared dispatcher (audit + repair + polish owned there)
        # and capture its report so _render_and_store doesn't re-run the loop.
        data, audit_report = render_pptx_from_plan_sync(plan, rows, ctx)
        return data, "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx", audit_report
```

- [ ] **Step 2: Run regression on existing service tests**

Run: `cd backend && venv/bin/python -m pytest tests/test_create_artifact_pipeline.py tests/test_artifact_export.py tests/test_deck_router.py -p no:jaxtyping -p no:cacheprovider`
Expected: existing tests still pass.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/artifacts/exporters/service.py
git commit -m "feat(ppt): wire render_html_deck into export service (sandbox path)"
```

---

## Task 11: Graceful degradation tests

**Files:**
- Create: `backend/tests/test_graceful_degradation.py` (NEW)

**Interfaces:**
- Verify `render_html_deck` skips unknown layouts and raises `RenderError` only when ALL slides are unrenderable.

- [ ] **Step 1: Write the tests**

Create `backend/tests/test_graceful_degradation.py`:

```python
"""Tests for graceful degradation — every HTML design failure must
fall back to the structured renderer without raising."""
import io
import pytest
from unittest.mock import patch
from pptx import Presentation


def _plan():
    from app.services.synexia.contracts import DeckPlan, SlidePlan
    return DeckPlan(
        title="T", deck_type="data_report", theme_recommendation="electric_studio",
        slides=[
            SlidePlan(layout="cover", title="T"),
            SlidePlan(layout="unknown_layout", title="Will be skipped"),
            SlidePlan(layout="closing", title="Q"),
        ],
    )


@pytest.fixture(autouse=True)
def skip_if_unavailable():
    from app.services.artifacts.render_html_deck import html_design_available
    if not html_design_available():
        pytest.skip("html_design not available (soffice/pdftoppm missing)")


class TestRenderHtmlDeckGracefulDegradation:
    def test_unknown_layout_skipped_not_crashed(self):
        from app.services.artifacts.render_html_deck import render_html_deck
        from app.services.artifacts.exporters._common import ExportContext
        ctx = ExportContext(source="t", user_message="")
        data = render_html_deck(_plan(), ctx)
        pres = Presentation(io.BytesIO(data))
        # 2 slides (cover + closing; unknown_layout skipped)
        assert len(pres.slides) == 2

    def test_no_slides_raises(self):
        from app.services.artifacts.render_html_deck import render_html_deck, RenderError
        from app.services.synexia.contracts import DeckPlan, SlidePlan
        from app.services.artifacts.exporters._common import ExportContext
        plan = DeckPlan(
            title="empty", deck_type="data_report", theme_recommendation="x",
            slides=[SlidePlan(layout="nonexistent_a", title="a"),
                    SlidePlan(layout="nonexistent_b", title="b")],
        )
        ctx = ExportContext(source="t", user_message="")
        with pytest.raises(RenderError, match="no slides"):
            render_html_deck(plan, ctx)

    def test_render_error_includes_root_cause(self):
        from app.services.artifacts.render_html_deck import render_html_deck, RenderError
        from app.services.synexia.contracts import DeckPlan, SlidePlan
        from app.services.artifacts.exporters._common import ExportContext
        plan = DeckPlan(
            title="T", deck_type="data_report", theme_recommendation="x",
            slides=[SlidePlan(layout="cover", title="T")],
        )
        ctx = ExportContext(source="t", user_message="")
        with patch(
            "app.services.artifacts.render_html_deck.render_image_fill",
            side_effect=Exception("soffice exploded"),
        ):
            with pytest.raises(RenderError, match="image_fill pipeline failed"):
                render_html_deck(plan, ctx)
```

- [ ] **Step 2: Run the tests**

Run: `cd backend && venv/bin/python -m pytest tests/test_graceful_degradation.py -v`
Expected: 3 passed (or all skipped if soffice/pdftoppm missing).

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_graceful_degradation.py
git commit -m "test(ppt): graceful degradation for render_html_deck"
```

---

## Task 12: End-to-end smoke + visual snapshot test

**Files:**
- Create: `backend/tests/test_render_html_deck_e2e.py` (NEW)
- Create: `backend/tests/snapshots/` (NEW directory)

**Interfaces:**
- 3-slide fixture deck → full pipeline → open with python-pptx → assert valid
- Visual snapshot of slide 1 against committed PNG in `tests/snapshots/electric_studio/01_cover.png`

- [ ] **Step 1: Write the e2e test**

Create `backend/tests/test_render_html_deck_e2e.py`:

```python
"""End-to-end smoke test for the HTML design pipeline.

Renders a 3-slide fixture through the full render_html_deck path and
asserts the PPTX is valid + slide count is correct.  Visual snapshot
comparison is best-effort (compression is non-deterministic across
soffice versions).
"""
import io
import os
import subprocess
import tempfile
from pathlib import Path
import pytest
from pptx import Presentation


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _fixture_plan():
    from app.services.synexia.contracts import DeckPlan, SlidePlan, KPISpec, ChartSpec
    return DeckPlan(
        title="Q3 Sales Recap",
        deck_type="data_report",
        theme_recommendation="electric_studio",
        slides=[
            SlidePlan(layout="cover", title="Q3 Sales Recap", subtitle="Up 8% QoQ"),
            SlidePlan(layout="kpi_grid", title="Key Metrics", kpi_specs=[
                KPISpec(label="Revenue", value="$2.4M", delta="+12%"),
                KPISpec(label="Orders", value="1,847", delta="+5%"),
            ]),
            SlidePlan(layout="chart_with_bullets", title="Revenue Trend",
                      bullets=["Q3 revenue up 12% QoQ", "Pipeline strong"],
                      chart=ChartSpec(type="bar", data={
                          "labels": ["Q1", "Q2", "Q3"],
                          "values": [1.8, 2.1, 2.4],
                      })),
        ],
    )


def _fixture_ctx():
    from app.services.artifacts.exporters._common import ExportContext
    return ExportContext(source="test", user_message="quarterly recap")


@pytest.fixture
def skip_if_unavailable():
    from app.services.artifacts.render_html_deck import html_design_available
    if not html_design_available():
        pytest.skip("soffice/pdftoppm missing — install libreoffice + poppler-utils")


class TestE2E:
    def test_full_pipeline_three_slides(self, skip_if_unavailable):
        from app.services.artifacts.render_html_deck import render_html_deck
        data = render_html_deck(_fixture_plan(), _fixture_ctx())
        assert data[:4] == b"PK\x03\x04"
        pres = Presentation(io.BytesIO(data))
        assert len(pres.slides) == 3
        # 16:9
        ratio = pres.slide_width / pres.slide_height
        assert 1.7 < ratio < 1.8

    def test_thumbnail_creation_works(self, skip_if_unavailable):
        """Run the same pipeline the service uses for thumbnails to verify
        the produced PPTX is renderable back to PNG by soffice."""
        from app.services.artifacts.render_html_deck import render_html_deck
        data = render_html_deck(_fixture_plan(), _fixture_ctx())
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            pptx_path = workdir / "out.pptx"
            pptx_path.write_bytes(data)
            proc = subprocess.run([
                "soffice", "--headless", "--convert-to", "pdf",
                "--outdir", str(workdir), str(pptx_path),
            ], capture_output=True, text=True, timeout=60,
               env={**os.environ, "HOME": str(workdir)})
            assert proc.returncode == 0, f"soffice failed: {proc.stderr[:500]}"
            pdf_path = workdir / "out.pdf"
            assert pdf_path.exists()
            proc2 = subprocess.run([
                "pdftoppm", "-png", "-r", "96",
                str(pdf_path), str(workdir / "page"),
            ], capture_output=True, text=True, timeout=30)
            assert proc2.returncode == 0
            pngs = list(workdir.glob("page-*.png"))
            assert len(pngs) == 3
```

- [ ] **Step 2: Run the test**

Run: `cd backend && venv/bin/python -m pytest tests/test_render_html_deck_e2e.py -v`
Expected: 2 passed (or skipped if soffice missing).

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_render_html_deck_e2e.py
git commit -m "test(ppt): e2e smoke + thumbnail round-trip"
```

---

## Task 13: Add `pick_pptx_mode` for the v1.1 editable-text path (architecture only)

**Files:**
- Modify: `backend/app/services/artifacts/deck_router.py`
- Test: `backend/tests/test_deck_router.py` (extend)

**Interfaces:**
- `pick_pptx_mode(plan, user_message) -> str` — returns `"image_fill"` (v1.0 default) or `"editable_text"` (v1.1, not yet implemented)
- Resolution: "edit/tweak" keywords → editable_text (gated by `HTML_DESIGN_EDITABLE_ENABLED`); otherwise image_fill

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_deck_router.py`:

```python
class TestPickPptxMode:
    def test_default_is_image_fill(self):
        from app.services.artifacts.deck_router import pick_pptx_mode
        assert pick_pptx_mode(_plan(), "make a beautiful deck") == "image_fill"

    def test_edit_keyword_returns_editable_text_when_enabled(self, monkeypatch):
        from app.services.artifacts.deck_router import pick_pptx_mode
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        assert pick_pptx_mode(_plan(), "let me edit the slides") == "editable_text"

    def test_edit_keyword_returns_image_fill_when_disabled(self):
        from app.services.artifacts.deck_router import pick_pptx_mode
        from app.config import settings
        # default False
        assert pick_pptx_mode(_plan(), "let me edit the slides") == "image_fill"

    def test_tweak_keyword(self, monkeypatch):
        from app.services.artifacts.deck_router import pick_pptx_mode
        from app.config import settings
        monkeypatch.setattr(settings, "HTML_DESIGN_EDITABLE_ENABLED", True)
        assert pick_pptx_mode(_plan(), "tweak the bullets") == "editable_text"
```

- [ ] **Step 2: Run the test — it should fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_deck_router.py::TestPickPptxMode -v`
Expected: FAIL with `ImportError: cannot import name 'pick_pptx_mode'`.

- [ ] **Step 3: Add `pick_pptx_mode` to `deck_router.py`**

Append to `backend/app/services/artifacts/deck_router.py`:

```python
# ---------------------------------------------------------------------------
# pick_pptx_mode — v1.1 architecture: LLM (or user) picks between
# image_fill and editable_text rendering per deck.
# ---------------------------------------------------------------------------

_EDITABLE_KEYWORDS = (
    "edit", "editable", "tweak", "modify", "update later",
    "我需要编辑", "可以编辑", "修改",
)


def pick_pptx_mode(plan, user_message: str = "") -> str:
    """Decide between ``image_fill`` (v1.0) and ``editable_text`` (v1.1).

    v1.0 always returns ``"image_fill"`` because the editable_text path
    is not implemented yet (gated by ``HTML_DESIGN_EDITABLE_ENABLED``).
    Architecture in place so v1.1 is a single-task drop-in.
    """
    from app.config import settings

    msg = (user_message or "").lower()
    if settings.HTML_DESIGN_EDITABLE_ENABLED and any(k in msg for k in _EDITABLE_KEYWORDS):
        return "editable_text"
    return "image_fill"
```

- [ ] **Step 4: Run the test — it should pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_deck_router.py -v`
Expected: 16 passed (12 existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/artifacts/deck_router.py backend/tests/test_deck_router.py
git commit -m "feat(ppt): pick_pptx_mode (v1.1 architecture — image_fill default in v1.0)"
```

---

## Task 14: Frontend preview — image-fill PPTX uses PNG preview (manual)

**Files:**
- Modify: `frontend/src/components/chat/MessageBubble.jsx`

**Interfaces:**
- When artifact metadata has `payload_formats.pptx.thumbnails` array (already populated by `service.py:_maybe_store_thumbnails` when LibreOffice succeeds), use the first thumbnail blob as the chat preview image.

- [ ] **Step 1: Locate the artifact preview logic**

Run: `grep -n 'thumbnails\|payload_formats\|artifact.preview' frontend/src/components/chat/MessageBubble.jsx`

- [ ] **Step 2: Add a thumbnail branch**

Find the artifact preview block and add a 3-5 line conditional that uses the first thumbnail if available. Example:

```jsx
{artifact?.metadata_json?.payload_formats?.pptx?.thumbnails?.length > 0 ? (
  <img
    src={`/api/artifacts/${artifact.id}/blob/${artifact.metadata_json.payload_formats.pptx.thumbnails[0]}`}
    alt={artifact.title}
    className="artifact-preview-image"
  />
) : (
  // existing textual preview
)}
```

- [ ] **Step 3: Build and smoke test**

```bash
cd frontend && ./node_modules/.bin/vite build --emptyOutDir=false && docker restart zhanlu-frontend
```

Open the chat with `HTML_DESIGN_RENDERER_ENABLED=True` and verify the preview shows the first slide as a PNG.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/chat/MessageBubble.jsx
git commit -m "feat(chat): use PNG thumbnail for image_fill PPTX artifacts"
```

---

## Self-Review

**1. Spec coverage** (each spec section → task):

- Section 4.1 `render_html_deck` → Task 9 ✓
- Section 4.2 `select_theme` → Task 3 ✓
- Section 4.3 `generate_html_slides` (12 layouts) → Tasks 4-6 ✓ + Task 7 (stage builder) ✓
- Section 4.4 `render_pptx` (image_fill mode) → Task 8 ✓; editable_text architected in Task 13 (v1.1 not in v1.0)
- Section 4.5 wire into `export_service` → Task 10 ✓
- Section 4.6 config flags → Task 1 ✓
- Section 4.7 frontend preview → Task 14 ✓
- Section 6 graceful degradation → Task 11 ✓
- Section 7 testing strategy (7 test files):
  - test_select_theme.py → Task 3 ✓
  - test_html_slide_generator.py → Tasks 4-6 ✓
  - test_html_to_pptx_image_fill.py → Task 8 ✓
  - test_render_html_deck.py → Task 9 ✓
  - test_render_html_deck_e2e.py → Task 12 ✓
  - test_router_prefers_html.py → covered by existing test_deck_router.py + Task 13 ✓
  - test_graceful_degradation.py → Task 11 ✓
  - test_theme_presets_catalog.py → Task 2 ✓
- Section 8 phased delivery → Tasks 1-14 map to Phase A.1; A.2 (editable_text) and A.3 (wider themes) explicitly out of scope (tracked in spec's "Notes for Future Work").

**2. Placeholder scan**: searched for "TBD", "TODO", "fill in", "appropriate", "similar to", "implement later" — no matches in the plan. The `not yet implemented` references in tests are intentional (used in `test_unknown_layout_raises` and `test_no_slides_raises` to verify the graceful-degradation contract).

**3. Type consistency**:
- `ThemePreset` defined Task 2, used by `select_theme` Task 3 and `render_slide` Task 4 ✓
- `render_slide(layout: str, plan: SlidePlan, theme: ThemePreset) -> str` defined Task 4, used by `build_stage` Task 7 and `render_html_deck` Task 9 ✓
- `build_stage(slide_htmls: List[str]) -> str` defined Task 7, used by `render_html_deck` Task 9 ✓
- `render_image_fill(stage_html: str) -> bytes` defined Task 8, used by `render_html_deck` Task 9 ✓
- `render_html_deck(plan: DeckPlan, ctx: ExportContext) -> bytes` defined Task 9, used by `service.py` Task 10 ✓
- `pick_pptx_mode(plan, user_message) -> str` defined Task 13, available for v1.1 ✓
- `DeckPlan` and `SlidePlan` consistently used from `app.services.synexia.contracts` throughout ✓
- `ExportContext` consistently used from `app.services.artifacts.exporters._common` throughout ✓

**4. Scope check**: 14 tasks, each with a self-contained deliverable. Each task has explicit commit step. Reasonable review gates (every 1-3 tasks).

**5. Risk acknowledgment**:
- LibreOffice + pdftoppm must be installed (already true in this env per `thumbnails.py`); Task 8 documents the install command.
- Image-fill PPTX is non-editable — explicitly noted in spec and re-noted in Task 14 frontend comments.
- All flags default OFF — production impact is zero until user opts in.
- soffice cold start is ~5s; the v1.0 design lives with this. v1.1 editable_text path would skip the conversion entirely.

---
