"""Deterministic smart router for deck rendering.

Decides whether a planned deck should render through the structured layout
engine (``"structured"``) or be handed to the design-heavy sandbox html2pptx
skill (``"sandbox"``).  No LLM call — fast and predictable.

Routing rules (in order):

1. A ``deck_type`` of ``investor_deck`` or ``marketing`` is design-heavy by
   nature → sandbox.
2. Any design/pitch keyword in the user message → sandbox.
3. If ``PPT_DESIGN_BY_DEFAULT`` is off → structured (legacy default, kept
   for backward compatibility).
4. Any explicit plain/data-dump keyword in the user message → structured.
5. Otherwise → sandbox (design is the default for pptx requests).

When ``PPT_SMART_ROUTER_ENABLED`` is off, the pipeline ignores this and always
renders structured — this module only *classifies*, it never forces a path.
"""

from __future__ import annotations

from typing import Optional

from app.services.synexia.contracts import DeckPlan

# Deck types that are design-heavy by nature and therefore route to the
# sandbox html2pptx skill (which gives the LLM full layout freedom).
_SANDBOX_DECK_TYPES = {"investor_deck", "marketing"}

# Keyword signals that the user wants a *designed* deck rather than a plain
# data report.  Lowercase; matched as substrings against the lowercased
# user message.
_SANDBOX_KEYWORDS = (
    # English
    "beautiful",
    "stunning",
    "gorgeous",
    "polished",
    "elegant",
    "premium",
    "designer",
    "pitch",
    "investor",
    "fundraise",
    "fundraising",
    "startup",
    "branding",
    "sales deck",
    "one-pager",
    "one pager",
    "teaser",
    # Chinese
    "精美",
    "漂亮",
    "好看",
    "设计感",
    "投资",
    "融资",
    "路演",
    "品牌",
    "营销",
    "招商",
)

# Keyword signals that the user wants a *plain* data report rather than a
# designed deck.  Lowercase; matched as substrings against the lowercased
# user message.  These override the design-by-default routing.
_STRUCTURED_KEYWORDS = (
    # English
    "plain",
    "simple text",
    "data dump",
    "text only",
    # Chinese
    "纯文本",
    "简单",
    "数据表",
)


def route_deck(plan: Optional[DeckPlan], user_message: str = "") -> str:
    """Classify a deck plan into ``"structured"`` or ``"sandbox"``.

    Parameters
    ----------
    plan:
        The planner's output (may be ``None`` — then the deck_type check is
        skipped and only the keyword signal is considered).
    user_message:
        The user's original request text, used for keyword signals.

    Returns
    -------
    ``"structured"`` or ``"sandbox"``.
    """
    deck_type = (plan.deck_type if plan else "") or ""
    deck_type = deck_type.strip().lower()

    if deck_type in _SANDBOX_DECK_TYPES:
        return "sandbox"

    msg = (user_message or "").lower()
    if any(k in msg for k in _SANDBOX_KEYWORDS):
        return "sandbox"

    from app.config import settings

    # Legacy default: when the operator explicitly disabled design-by-default,
    # fall back to the old behavior (structured unless a design signal fired).
    if not settings.PPT_DESIGN_BY_DEFAULT:
        return "structured"

    # Explicit plain / data-dump intent wins over the design default.
    if any(k in msg for k in _STRUCTURED_KEYWORDS):
        return "structured"

    return "sandbox"


# ---------------------------------------------------------------------------
# Phase 4 — deck profile classification
# ---------------------------------------------------------------------------

from app.services.artifacts.deck_profiles import (  # noqa: E402
    ALL_PROFILES,
    classify_profile as _classify_profile_deterministic,
    get_profile,
)

# Optional LLM fallback prompt used only when keywords are ambiguous and the
# flag is enabled.  Bounded by a 10s timeout so it never blocks the pipeline.
_PROFILE_LLM_TIMEOUT_S = 10.0

_PROFILE_LLM_PROMPT = (
    "Classify the user's presentation request into exactly one of these deck "
    "profiles: data_report, executive_brief, pitch_narrative, periodic_review. "
    "Respond with ONLY the profile name, no punctuation.\n"
    "Definitions:\n"
    "- data_report: general analytical data presentation.\n"
    "- executive_brief: short (3-5 slide) leadership summary, no raw tables.\n"
    "- pitch_narrative: persuasive story arc to sell an idea/investor.\n"
    "- periodic_review: recurring weekly/monthly/quarterly status review.\n"
    "REQUEST: {request}"
)


async def classify_profile(
    user_intent: str,
    explicit: Optional[str] = None,
    allow_llm_fallback: bool = False,
) -> str:
    """Classify a user intent into a deck profile name.

    Resolution order:
      1. An explicit (already-validated) profile name wins unconditionally.
      2. Deterministic keyword rules (fast, always available).
      3. If ``allow_llm_fallback`` and no keyword fired, a single bounded LLM
         call (10s) disambiguates; on any failure/ambiguity, default
         ``data_report``.

    Returns the profile *name* (a key of ``ALL_PROFILES``).
    """
    # Step 1 — explicit always wins.
    if explicit:
        key = explicit.strip().lower()
        if key in ALL_PROFILES:
            return key

    # Step 2 — deterministic keyword rules.
    det = _classify_profile_deterministic(user_intent)
    if det.name != "data_report" or not allow_llm_fallback:
        # Either we got a specific profile, or LLM fallback isn't allowed →
        # accept the deterministic result (data_report is the safe default).
        return det.name

    # Step 3 — bounded LLM fallback (only when ambiguous + enabled).
    try:
        from app.config import settings
        if not settings.DECK_PROFILES_ENABLED:
            return det.name
        from app.services.llm_service import call_llm

        result = await asyncio.wait_for(
            call_llm(
                prompt=_PROFILE_LLM_PROMPT.format(request=user_intent or ""),
                temperature=0.0,
                task_type="deck_profile",
            ),
            timeout=_PROFILE_LLM_TIMEOUT_S,
        )
        data = result.get("data") if isinstance(result, dict) else None
        text = (data or {}).get("response") if isinstance(data, dict) else None
        if isinstance(text, str):
            name = text.strip().strip('"').lower()
            if name in ALL_PROFILES:
                return name
    except Exception as exc:  # noqa: BLE001 — fallback is best-effort
        logger.warning("deck_router: profile LLM fallback failed: %s", exc)
    return det.name  # safe default


def get_deck_profile(name: str):
    """Return the DeckProfile dataclass for a name (default data_report)."""
    return get_profile(name)


import asyncio  # noqa: E402  (imported late to keep module-level cheap)
import logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pick_pptx_mode — v1.1 architecture: LLM (or user) picks between
# image_fill and editable_text rendering per deck.
# ---------------------------------------------------------------------------

# Users overwhelmingly expect a downloaded deck to be editable in PowerPoint.
# With HTML_DESIGN_EDITABLE_ENABLED, editable_text (native text frames via
# slide-skill) is the DEFAULT for sandbox-routed decks; image_fill (baked PNGs)
# is reserved for explicit requests for a static/picture deck. Reversed
# 2026-08-29 after the user complaint "the ppt is not editable after download".
_IMAGE_KEYWORDS = (
    "static", "image", "as picture", "as image", "not editable",
    "keep the design", "picture only", "锁定", "静态", "图片形式", "不要编辑",
)


def pick_pptx_mode(plan, user_message: str = "") -> str:
    """Decide between ``editable_text`` (v1.1, default) and ``image_fill`` (v1.0).

    ``editable_text`` produces a native .pptx with REAL text frames — every
    shape editable in PowerPoint.  ``image_fill`` bakes each slide as a
    1920x1080 PNG (beautiful, static).  Default is editable_text whenever
    ``HTML_DESIGN_EDITABLE_ENABLED`` is on; the caller additionally guards on
    ``slideskill_bridge.editable_available()`` and falls back to image_fill
    on any slide-skill failure (never blocks a deck).
    """
    from app.config import settings

    if not settings.HTML_DESIGN_EDITABLE_ENABLED:
        return "image_fill"
    msg = (user_message or "").lower()
    if any(k in msg for k in _IMAGE_KEYWORDS):
        return "image_fill"
    return "editable_text"
