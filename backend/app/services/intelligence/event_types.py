"""Event data structures for the EDIA Intelligence Layer.

Defines ExtractedEvent — the canonical representation of a market-moving
event detected from news, RSS, or manual submission.

Ported from EDIA 5.1.2 — pure Python, no external dependencies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


# ── Magnitude string → float mapping ─────────────────────────────────────
_MAGNITUDE_SCORE = {
    "small": 0.15,
    "medium": 0.40,
    "large": 0.70,
    "extreme": 0.95,
}

_CERTAINTY_SCORE = {
    "confirmed": 0.95,
    "likely": 0.75,
    "rumored": 0.45,
    "speculative": 0.25,
}

_RELEVANCE_SCORE = {
    "direct": 0.90,
    "indirect": 0.60,
    "minimal": 0.20,
}


@dataclass
class ExtractedEvent:
    """Canonical market event extracted from a news article or external feed."""

    event_type: (
        str  # geopolitical_supply_disruption | plant_outage | sinopec_price_change
    )
    # tariff_policy | demand_signal | shipping_disruption | opec_decision
    # weather_event | currency_move | sanctions

    headline: str  # one-sentence summary
    affected_commodities: list[str]  # crude_oil, naphtha, c5, isoprene …
    direction: str  # bullish | bearish | neutral | uncertain
    magnitude_estimate: str  # small | medium | large | extreme
    certainty: str  # confirmed | likely | rumored | speculative
    time_horizon: str  # immediate(0-3d) | short(3-14d) | medium(14-60d)
    geographic_scope: str  # global | asia_pacific | china | middle_east | europe
    relevance_to_c5_c9: str  # direct | indirect | minimal

    # optional enrichment
    key_entities: list[str] = field(default_factory=list)
    key_information: str = ""
    causal_chain_hint: str = ""
    source_url: str = ""
    source_credibility: str = "tier2"  # tier1 | tier2 | tier3
    full_text: str = ""
    raw_quote: str = ""

    # moderation + feedback
    review_status: str = "approved"  # pending | approved | rejected
    usefulness_score: int = 0  # 0-5 admin feedback score
    reviewed_by: str = ""
    reviewed_at: str = ""
    review_note: str = ""

    # auto-computed
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    detected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # ── Derived scores ────────────────────────────────────────────────────
    @property
    def impact_magnitude(self) -> float:
        """0-1 score combining magnitude + certainty + relevance."""
        mag = _MAGNITUDE_SCORE.get(self.magnitude_estimate, 0.3)
        cert = _CERTAINTY_SCORE.get(self.certainty, 0.5)
        rel = _RELEVANCE_SCORE.get(self.relevance_to_c5_c9, 0.3)
        return round(mag * 0.40 + cert * 0.35 + rel * 0.25, 3)

    @property
    def relevance_score(self) -> float:
        return _RELEVANCE_SCORE.get(self.relevance_to_c5_c9, 0.3)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["impact_magnitude"] = self.impact_magnitude
        d["relevance_score"] = self.relevance_score
        return d

    # convenience
    def __str__(self) -> str:
        return (
            f"[{self.event_type}] {self.headline} "
            f"(impact={self.impact_magnitude:.2f}, dir={self.direction})"
        )
