"""Phase 4 — Intent-driven deck profiles.

A *deck profile* constrains how a deck is planned and rendered for a given
user intent.  Four profiles are defined:

    * ``data_report``      — default; data-dense report (unchanged behavior).
    * ``executive_brief``  — tight 3-5 slide exec summary, no raw tables.
    * ``pitch_narrative``  — story arc: hook → problem → evidence → ask.
    * ``periodic_review``  — recurring periodic review (deltas + trend).

Each profile is a small dataclass carrying structural constraints that the
planner (``deck_planner.py``) and layout engine read.  Profiles are purely
advisory — they never change the rendering pipeline, only *what* slides get
produced and how dense they are.

Classification (``classify_profile``) is deterministic keyword matching first,
with an optional LLM fallback (10s timeout) used only when the keywords are
ambiguous.  An explicit user instruction always wins over classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Profile contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeckProfile:
    """Structural constraints for a deck intent."""

    name: str
    description: str
    slide_count_range: tuple[int, int]          # (min, max) inclusive
    allowed_layouts: tuple[str, ...]            # layouts the planner may use
    density_budget: int                        # soft cap on total content units
    headline_style: str                        # default headline tone
    preferred_chart_types: tuple[str, ...]     # charts the planner should favor
    mood_words: tuple[str, ...]                # copy-tone hints
    forbid_tables: bool = False                # drop raw data_table slides
    require_deltas: bool = False               # prefer delta/KPI-delta framing


# ---------------------------------------------------------------------------
# The four profiles
# ---------------------------------------------------------------------------

DATA_REPORT = DeckProfile(
    name="data_report",
    description="General data-dense report — the historical default behavior.",
    slide_count_range=(4, 12),
    allowed_layouts=(
        "cover", "kpi_grid", "chart_full", "chart_with_bullets",
        "data_table", "findings_cards", "insights_bullets", "section_divider",
        "recommendations", "closing",
        "timeline", "roadmap", "comparison", "swot", "process_flow",
    ),
    density_budget=40,
    headline_style="assertion",
    preferred_chart_types=("bar", "line", "column"),
    mood_words=("analytical", "precise", "factual"),
)

EXECUTIVE_BRIEF = DeckProfile(
    name="executive_brief",
    description="Tight executive summary — 3-5 slides, no raw tables.",
    slide_count_range=(3, 5),
    allowed_layouts=(
        "cover", "kpi_grid", "chart_with_bullets", "findings_cards",
        "recommendations", "closing",
        "timeline", "comparison", "swot", "quote",
    ),
    density_budget=14,
    headline_style="assertion",
    preferred_chart_types=("bar", "line", "column", "donut"),
    mood_words=("authoritative", "decisive", "concise"),
    forbid_tables=True,
)

PITCH_NARRATIVE = DeckProfile(
    name="pitch_narrative",
    description="Persuasive story arc: hook → problem → evidence → ask.",
    slide_count_range=(5, 8),
    allowed_layouts=(
        "cover", "section_divider", "findings_cards", "chart_with_bullets",
        "recommendations", "closing",
        "timeline", "roadmap", "comparison", "quote", "process_flow",
    ),
    density_budget=22,
    headline_style="question",
    preferred_chart_types=("bar", "line", "column"),
    mood_words=("compelling", "visionary", "aspirational"),
)

PERIODIC_REVIEW = DeckProfile(
    name="periodic_review",
    description="Recurring periodic review — deltas + trend emphasis.",
    slide_count_range=(4, 7),
    allowed_layouts=(
        "cover", "kpi_grid", "chart_full", "insights_bullets",
        "recommendations", "closing",
        "timeline", "comparison",
    ),
    density_budget=24,
    headline_style="assertion",
    preferred_chart_types=("line", "column", "bar"),
    mood_words=("reflective", "measured", "steady"),
    require_deltas=True,
)


ALL_PROFILES: dict[str, DeckProfile] = {
    p.name: p for p in (DATA_REPORT, EXECUTIVE_BRIEF, PITCH_NARRATIVE, PERIODIC_REVIEW)
}

DEFAULT_PROFILE = DATA_REPORT


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Keyword rules: each profile maps to trigger tokens.  Order matters only in
# that earlier (more specific) profiles win on ties; data_report is the
# catch-all and always matches last.
_KEYWORD_RULES: tuple[tuple[DeckProfile, tuple[str, ...]], ...] = (
    (EXECUTIVE_BRIEF, (
        "executive", "exec summary", "executive summary", "leadership", "board",
        "c-level", "c suite", "one-pager", "one pager", "brief",
    )),
    (PITCH_NARRATIVE, (
        "pitch", "investor", "fundraise", "fundraising", "raise", "deck",
        "proposal", "sell", "persuade", "story", "narrative", "vision",
    )),
    (PERIODIC_REVIEW, (
        "weekly", "monthly", "quarterly", "annual", "review", "retrospective",
        "status", "roundup", "wrap-up", "recap", "periodic",
    )),
    (DATA_REPORT, (
        "report", "analysis", "analyze", "dataset", "data report", "findings",
        "summary", "overview", "dashboard", "breakdown",
    )),
)


def classify_profile(
    user_intent: str,
    explicit: Optional[str] = None,
) -> DeckProfile:
    """Classify a user intent into a DeckProfile.

    An explicit profile name (already validated) always wins.  Otherwise the
    deterministic keyword rules are applied; if no rule fires, the default
    ``data_report`` profile is returned.

    Note: the LLM fallback (when ambiguous and LLM enabled) is invoked by the
    caller (``deck_router.classify_profile``) — this function stays pure and
    deterministic so it is trivially testable.
    """
    if explicit:
        key = explicit.strip().lower()
        if key in ALL_PROFILES:
            return ALL_PROFILES[key]

    text = (user_intent or "").lower()
    best: Optional[DeckProfile] = None
    best_score = 0
    for profile, keywords in _KEYWORD_RULES:
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best = profile
    return best or DEFAULT_PROFILE


def get_profile(name: str) -> DeckProfile:
    """Return a profile by name, falling back to the default."""
    return ALL_PROFILES.get(name, DEFAULT_PROFILE)
