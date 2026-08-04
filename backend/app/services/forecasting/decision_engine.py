"""Buy/hold/sell decision engine — Phase E Task E2.

Combines:
  - ``p_rise``: probability of price rising (from Phase D conformal probability)
  - ``expected_change_pct``: expected % change (from Phase D)
  - ``directional_acc`` / ``directional_status``: from Phase E1 classifier
  - ``trust_tier``: from `forecast_trust_tier.py` (high/medium/directional/low)

Decision logic:
  - "watch" (low confidence) when no statistical edge OR low trust tier
  - "buy" when p_rise ≥ 0.70 AND expected_change > 3% AND has edge
  - "sell" when p_rise ≤ 0.30 AND expected_change < -3% AND has edge
  - "hold" otherwise

Confidence:
  - "high" only when trust_tier=high AND |p_rise - 0.5| > 0.25
  - "medium" for actionable signals with directional but not high trust
  - "low" for everything else (including all "watch" decisions)
"""
from __future__ import annotations

from dataclasses import dataclass

_BUY_THRESHOLD = 0.70
_SELL_THRESHOLD = 0.30
_BUY_MIN_CHANGE = 0.03   # 3% expected rise
_SELL_MIN_CHANGE = -0.03 # 3% expected fall
_EDGE_THRESHOLD = 0.55   # minimum directional accuracy to act on
_P_HIGH_MARGIN = 0.25    # |p_rise - 0.5| needed for "high" confidence


@dataclass
class Decision:
    action: str          # "buy" | "hold" | "sell" | "watch"
    confidence: str      # "high" | "medium" | "low"
    rationale: str       # human-readable explanation


def recommend(
    p_rise: float,
    expected_change_pct: float,
    directional_acc: float | None,
    directional_status: str | None,
    trust_tier: str,
) -> Decision:
    """Compute buy/hold/sell recommendation.

    Parameters
    ----------
    p_rise : float
        Probability that price[T+h] > price[T]. From Phase D.
    expected_change_pct : float
        Point-forecast expected % change. From Phase D.
    directional_acc : float | None
        Walk-forward directional accuracy. None when classifier unavailable.
    directional_status : str | None
        "edge" / "no_edge" from Phase E1 classifier.
    trust_tier : str
        "high" / "medium" / "directional" / "low".

    Returns
    -------
    Decision with action, confidence, rationale.
    """
    tier = (trust_tier or "low").lower()
    has_edge = (
        directional_status == "edge"
        and directional_acc is not None
        and directional_acc >= _EDGE_THRESHOLD
    )

    # Watch zone: no edge, low trust, or any other hedging condition
    if not has_edge or tier == "low":
        msg = (
            "No reliable directional edge; insufficient confidence for a "
            "trade signal."
        )
        if tier == "low":
            msg = "Low trust tier — watch only."
        elif not has_edge:
            msg = "No statistically significant directional edge — watch only."
        return Decision("watch", "low", msg)

    # Determine confidence from trust tier + probability margin
    p_margin = abs(p_rise - 0.5)
    if tier == "high" and p_margin >= _P_HIGH_MARGIN:
        conf = "high"
    elif tier in ("high", "medium"):
        conf = "medium"
    else:  # "directional" tier or anything else actionable
        conf = "medium"

    # Actionable signals
    if p_rise >= _BUY_THRESHOLD and expected_change_pct > _BUY_MIN_CHANGE:
        return Decision(
            "buy", conf,
            f"P(rise)={p_rise:.0%}, expected +{expected_change_pct:.1%}; "
            f"directional accuracy {directional_acc:.0%}.",
        )
    if p_rise <= _SELL_THRESHOLD and expected_change_pct < _SELL_MIN_CHANGE:
        return Decision(
            "sell", conf,
            f"P(rise)={p_rise:.0%}, expected {expected_change_pct:.1%}; "
            f"directional accuracy {directional_acc:.0%}.",
        )
    return Decision(
        "hold", conf,
        f"P(rise)={p_rise:.0%}, expected {expected_change_pct:+.1%}; "
        f"no actionable move.",
    )