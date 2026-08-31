"""Deterministic per-slide DeckPlan mutation functions (PHASE 2).

This is the PURE edit layer: every function returns a NEW ``DeckPlan``
(via ``model_copy(deep=True)``) and never mutates the input plan.  The
tool-handler layer (``deck_edit_tool.py``) is responsible for auth,
loading the plan from ``ArtifactVersion.source_json``, re-rendering,
versioning, and thumbnails.

Safety invariants enforced here:

* ``remove_slide`` refuses the cover slide (index 0) and the closing
  slide (last index).
* ``reorder_slide`` keeps the cover at index 0 and the closing at the
  last index.
* Field edits go through whitelists so the LLM cannot mutate structural
  fields (e.g. ``layout``, ``chart_spec`` via edit_slide).
* ``add_slide`` validates the ``layout`` value against the known layouts
  the layout engine can render.

Functions are pure and side-effect free, which keeps them trivially
unit-testable.
"""

from __future__ import annotations

from typing import Any, Optional

from app.services.artifacts.exporters._theme import validate_theme_name
from app.services.synexia.contracts import (
    ChartSpecInSlide,
    DeckPlan,
    KPISpecInSlide,
    SlidePlan,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The layouts the layout engine can render (mirrors SlidePlan.layout docs).
ALLOWED_LAYOUTS: frozenset[str] = frozenset({
    "cover",
    "agenda",
    "kpi_grid",
    "chart_full",
    "chart_with_bullets",
    "findings_cards",
    "insights_bullets",
    "recommendations",
    "data_table",
    "methodology",
    "section_divider",
    "closing",
    # New archetypes (2026-08-29) — renderers exist in both HTML + structured
    "timeline",
    "roadmap",
    "comparison",
    "swot",
    "quote",
    "process_flow",
})

# Whitelist of slide fields edit_slide may patch.
_EDITABLE_SLIDE_FIELDS: frozenset[str] = frozenset({
    "title",
    "subtitle",
    "bullets",
    "notes",
    "kpi_specs",
})

# Whitelist of deck fields restyle_deck may patch.
_RESTYLE_FIELDS: frozenset[str] = frozenset({
    "theme_recommendation",
    "headline_style",
    "summary",
    "methodology",
})

# Whitelist of chart fields update_chart may patch.
_CHART_FIELDS: frozenset[str] = frozenset({
    "chart_type",
    "x_key",
    "y_keys",
    "title",
})


class DeckEditError(ValueError):
    """Raised for invalid edit operations (bad index, refused slide, bad schema)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy(plan: DeckPlan) -> DeckPlan:
    """Return a deep copy of the plan so mutation never touches the input."""
    return plan.model_copy(deep=True)


def _require_index(plan: DeckPlan, slide_index: int, where: str = "slide_index") -> None:
    """Validate ``slide_index`` is within the current slide list."""
    n = len(plan.slides)
    if n == 0:
        raise DeckEditError("deck has no slides to edit")
    if not isinstance(slide_index, int) or isinstance(slide_index, bool):
        raise DeckEditError(f"{where} must be an integer, got {slide_index!r}")
    if slide_index < 0 or slide_index >= n:
        raise DeckEditError(
            f"{where} {slide_index} out of range (deck has {n} slides, indexes 0..{n - 1})"
        )


def _normalize_index(plan: DeckPlan, slide_index: Optional[int]) -> int:
    """Default ``add_slide`` to inserting right before the closing slide."""
    if slide_index is None:
        return max(len(plan.slides) - 1, 0)
    return slide_index


def _check_only_fields(fields: dict, allowed: frozenset[str], op: str) -> None:
    unknown = set(fields) - set(allowed)
    if unknown:
        raise DeckEditError(
            f"{op}: unknown field(s) {sorted(unknown)}; allowed: {sorted(allowed)}"
        )


# ---------------------------------------------------------------------------
# edit_slide — patch a single slide's content
# ---------------------------------------------------------------------------


def edit_slide(plan: DeckPlan, slide_index: int, changes: dict) -> DeckPlan:
    """Patch whitelisted fields of the slide at ``slide_index``.

    ``changes`` may contain: title, subtitle, bullets, notes, kpi_specs.
    ``kpi_specs`` is validated into ``KPISpecInSlide`` models.
    """
    if not isinstance(changes, dict) or not changes:
        raise DeckEditError("edit_slide requires a non-empty 'changes' object")
    _check_only_fields(changes, _EDITABLE_SLIDE_FIELDS, "edit_slide")
    out = _copy(plan)
    _require_index(out, slide_index)
    slide = out.slides[slide_index]

    for key, value in changes.items():
        if key == "bullets":
            if not isinstance(value, list):
                raise DeckEditError("edit_slide: 'bullets' must be a list of strings")
            slide.bullets = [str(b) for b in value]
        elif key == "kpi_specs":
            if not isinstance(value, list):
                raise DeckEditError("edit_slide: 'kpi_specs' must be a list")
            slide.kpi_specs = [KPISpecInSlide.model_validate(v) for v in value]
        else:
            setattr(slide, key, value)
    return out


# ---------------------------------------------------------------------------
# add_slide — insert a new slide
# ---------------------------------------------------------------------------


def add_slide(plan: DeckPlan, slide_index: Optional[int], slide: dict) -> DeckPlan:
    """Insert a new slide described by ``slide``.

    ``slide.layout`` is validated against ``ALLOWED_LAYOUTS``.  When
    ``slide_index`` is None the slide is inserted right before the closing
    slide (or appended when the deck has no closing slide yet).
    """
    if not isinstance(slide, dict) or "layout" not in slide or "title" not in slide:
        raise DeckEditError("add_slide requires 'slide' with at least 'layout' and 'title'")
    if slide["layout"] not in ALLOWED_LAYOUTS:
        raise DeckEditError(
            f"add_slide: invalid layout {slide['layout']!r}; allowed: {sorted(ALLOWED_LAYOUTS)}"
        )
    out = _copy(plan)
    new_slide = SlidePlan.model_validate(slide)
    idx = _normalize_index(out, slide_index)
    if idx < 0 or idx > len(out.slides):
        raise DeckEditError(f"add_slide: slide_index {idx} out of range (0..{len(out.slides)})")
    out.slides.insert(idx, new_slide)
    return out


# ---------------------------------------------------------------------------
# restyle_deck — update deck-wide styling / narrative fields
# ---------------------------------------------------------------------------


def restyle_deck(plan: DeckPlan, changes: dict) -> DeckPlan:
    """Update whitelisted deck fields: theme_recommendation, headline_style,
    summary, methodology.

    ``theme_recommendation`` is validated against the vendored theme library
    (with alias resolution). An unknown theme raises ``DeckEditError`` listing
    the available themes — the existing ``load_theme`` fallback to
    ``zhanlu-blue`` only applies at *render* time, so we surface the typo
    here rather than silently rendering the wrong deck.
    """
    if not isinstance(changes, dict) or not changes:
        raise DeckEditError("restyle_deck requires a non-empty 'changes' object")
    _check_only_fields(changes, _RESTYLE_FIELDS, "restyle_deck")

    if "theme_recommendation" in changes:
        try:
            changes["theme_recommendation"] = validate_theme_name(
                changes["theme_recommendation"]
            )
        except ValueError as exc:
            raise DeckEditError(str(exc)) from exc

    out = _copy(plan)
    for key, value in changes.items():
        setattr(out, key, value)
    return out


# ---------------------------------------------------------------------------
# update_chart — update the chart spec of a single slide
# ---------------------------------------------------------------------------


def update_chart(plan: DeckPlan, slide_index: int, chart: dict) -> DeckPlan:
    """Update the target slide's ``chart_spec``.

    ``chart`` may contain: chart_type, x_key, y_keys, title.  If the slide
    has no chart_spec yet, one is created (defaults ``bar``/``""``).
    """
    if not isinstance(chart, dict) or not chart:
        raise DeckEditError("update_chart requires a non-empty 'chart' object")
    _check_only_fields(chart, _CHART_FIELDS, "update_chart")
    out = _copy(plan)
    _require_index(out, slide_index)
    slide = out.slides[slide_index]

    current = slide.chart_spec
    payload = {
        "chart_type": current.chart_type if current else "bar",
        "x_key": current.x_key if current else "",
        "y_keys": list(current.y_keys) if current else [],
        "title": current.title if current else "",
    }
    for key, value in chart.items():
        if key == "y_keys":
            if not isinstance(value, list):
                raise DeckEditError("update_chart: 'y_keys' must be a list")
            payload["y_keys"] = [str(v) for v in value]
        else:
            payload[key] = value
    slide.chart_spec = ChartSpecInSlide.model_validate(payload)
    return out


# ---------------------------------------------------------------------------
# remove_slide — delete a slide (cover/closing protected)
# ---------------------------------------------------------------------------


def remove_slide(plan: DeckPlan, slide_index: int) -> DeckPlan:
    """Remove the slide at ``slide_index``.

    Refuses the cover slide (index 0) and the closing slide (last index).
    """
    out = _copy(plan)
    _require_index(out, slide_index)
    n = len(out.slides)
    if slide_index == 0:
        raise DeckEditError("cannot remove the cover slide (index 0)")
    if slide_index == n - 1:
        raise DeckEditError("cannot remove the closing slide (last index)")
    del out.slides[slide_index]
    return out


# ---------------------------------------------------------------------------
# reorder_slide — move a slide (cover/closing stay anchored)
# ---------------------------------------------------------------------------


def reorder_slide(plan: DeckPlan, from_index: int, to_index: int) -> DeckPlan:
    """Move the slide at ``from_index`` to ``to_index``.

    The cover slide must remain at index 0 and the closing slide at the
    last index; attempting to move either (as source or destination)
    raises ``DeckEditError``.
    """
    out = _copy(plan)
    n = len(out.slides)
    _require_index(out, from_index, "from_index")
    _require_index(out, to_index, "to_index")
    if n <= 2:
        raise DeckEditError("reorder_slide: deck has too few movable slides")
    if from_index == 0 or to_index == 0:
        raise DeckEditError("reorder_slide: the cover slide (index 0) cannot be moved")
    if from_index == n - 1 or to_index == n - 1:
        raise DeckEditError("reorder_slide: the closing slide (last index) cannot be moved")

    slide = out.slides.pop(from_index)
    # ``to_index`` is the FINAL position in the resulting list, so no
    # adjustment after the pop is needed: inserting at ``to_index`` puts the
    # moved slide exactly where requested in both directions.
    out.slides.insert(to_index, slide)
    return out
