"""Deterministic smart router for deck rendering.

Decides whether a planned deck should render through the structured layout
engine (``"structured"``) or be handed to the design-heavy sandbox html2pptx
skill (``"sandbox"``).  No LLM call — fast and predictable.

Routing rules (in order):

1. A ``deck_type`` of ``investor_deck`` or ``marketing`` is design-heavy by
   nature → sandbox.
2. Any design/pitch keyword in the user message → sandbox.
3. Otherwise → structured.

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

    return "structured"
