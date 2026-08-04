"""Tests for the decision engine (Phase E Task E2)."""
from __future__ import annotations

import pytest

from app.services.forecasting.decision_engine import recommend, Decision


def test_buy_on_high_probability_high_trust():
    d = recommend(
        p_rise=0.78, expected_change_pct=0.06,
        directional_acc=0.62, directional_status="edge",
        trust_tier="high",
    )
    assert d.action == "buy"
    assert d.confidence in ("high", "medium")


def test_hold_on_ambiguous():
    # Edge exists but probability is near 50% → no actionable move → "hold".
    d = recommend(
        p_rise=0.52, expected_change_pct=0.01,
        directional_acc=0.56, directional_status="edge",
        trust_tier="medium",
    )
    assert d.action == "hold"


def test_sell_on_low_probability():
    d = recommend(
        p_rise=0.20, expected_change_pct=-0.05,
        directional_acc=0.60, directional_status="edge",
        trust_tier="high",
    )
    assert d.action == "sell"


def test_watch_when_no_edge():
    d = recommend(
        p_rise=0.55, expected_change_pct=0.02,
        directional_acc=0.50, directional_status="no_edge",
        trust_tier="low",
    )
    assert d.action == "watch"
    assert "no" in d.rationale.lower() or "low" in d.rationale.lower()


def test_watch_when_no_edge_even_with_high_p():
    """Strong probability but no statistical edge → still watch."""
    d = recommend(
        p_rise=0.85, expected_change_pct=0.08,
        directional_acc=0.50, directional_status="no_edge",
        trust_tier="high",
    )
    assert d.action == "watch"
    assert d.confidence == "low"


def test_watch_when_low_trust_tier():
    """Low trust tier overrides even a real edge."""
    d = recommend(
        p_rise=0.78, expected_change_pct=0.06,
        directional_acc=0.62, directional_status="edge",
        trust_tier="low",
    )
    assert d.action == "watch"
    assert d.confidence == "low"


def test_high_confidence_requires_high_tier_and_margin():
    """High confidence only when trust=high AND |p_rise-0.5| > 0.25."""
    d_high = recommend(
        p_rise=0.85, expected_change_pct=0.10,
        directional_acc=0.65, directional_status="edge",
        trust_tier="high",
    )
    assert d_high.confidence == "high"

    # Same probability but medium trust → medium confidence
    d_med = recommend(
        p_rise=0.85, expected_change_pct=0.10,
        directional_acc=0.65, directional_status="edge",
        trust_tier="medium",
    )
    assert d_med.confidence == "medium"


def test_decision_dataclass_fields():
    d = recommend(
        p_rise=0.75, expected_change_pct=0.05,
        directional_acc=0.60, directional_status="edge",
        trust_tier="medium",
    )
    assert isinstance(d, Decision)
    assert hasattr(d, "action")
    assert hasattr(d, "confidence")
    assert hasattr(d, "rationale")
    assert isinstance(d.rationale, str)
    assert len(d.rationale) > 0