"""Tests for EDIA-ported domain signals: elasticities + seasonal rules."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.forecasting.domain_signals import (
    _ELASTICITIES,
    _RAW_ELASTICITIES,
    _SEASONAL_RULES,
    compute_causal_chain_adjustment,
    compute_domain_signal_adjustment,
    compute_seasonal_adjustment,
)


# ── Elasticity constants ────────────────────────────────────────────────────

def test_elasticities_dict_has_known_products():
    """The elasticity table should include the petrochem chain products."""
    for pid in ("naphtha", "cracked_c5", "isoprene", "piperylene",
                "dcpd", "c5_resin", "raffinate_c5", "sbs"):
        assert pid in _ELASTICITIES, f"{pid} missing from _ELASTICITIES"


def test_elasticities_are_raw_times_dampening():
    """Each effective coefficient = raw × tier_dampening, rounded to 4 dp."""
    for pid, (raw, damp) in _RAW_ELASTICITIES.items():
        expected = round(raw * damp, 4)
        assert _ELASTICITIES[pid] == expected, (
            f"{pid}: {raw}×{damp}={expected} but _ELASTICITIES has {_ELASTICITIES[pid]}"
        )


def test_naphtha_elasticity_is_unity():
    """Naphtha is the root of the causal chain — elasticity 1.0."""
    assert _ELASTICITIES["naphtha"] == 1.0


def test_c5_resin_tier3_dampening():
    """c5_resin is Tier 3: raw 0.62 × damp 0.72 = 0.4464."""
    assert _RAW_ELASTICITIES["c5_resin"] == (0.62, 0.72)
    assert _ELASTICITIES["c5_resin"] == 0.4464


def test_isoprene_tier2_dampening():
    """isoprene is Tier 2: raw 0.88 × damp 0.85 = 0.748."""
    assert _ELASTICITIES["isoprene"] == 0.748


# ── Seasonal rules ──────────────────────────────────────────────────────────

def test_seasonal_rules_cover_c5_resin_full_year():
    """c5_resin should have a rule for every month 1-12."""
    c5_months = {m for (pid, m) in _SEASONAL_RULES if pid == "c5_resin"}
    assert c5_months == set(range(1, 13)), (
        f"c5_resin seasonal rules missing months: {set(range(1,13)) - c5_months}"
    )


def test_seasonal_c5_resin_winter_is_negative():
    """c5_resin Nov-Feb should be -2.5% (winter demand dip)."""
    for month in (11, 12, 1, 2):
        assert _SEASONAL_RULES[("c5_resin", month)] == -2.5


def test_seasonal_c5_resin_spring_summer_is_positive():
    """c5_resin Mar-Oct should be +1.0%."""
    for month in range(3, 11):
        assert _SEASONAL_RULES[("c5_resin", month)] == 1.0


def test_seasonal_adjustment_returns_zero_for_unknown_product():
    """Unknown product → 0.0 (no adjustment)."""
    assert compute_seasonal_adjustment("unknown_product", 7) == 0.0


def test_seasonal_adjustment_returns_zero_for_unrule_month():
    """Product with no rule for the given month → 0.0."""
    # naphtha has no seasonal rules at all
    assert compute_seasonal_adjustment("naphtha", 7) == 0.0


def test_seasonal_adjustment_returns_pct_for_known_combo():
    """Known (product, month) → the rule's pct value."""
    assert compute_seasonal_adjustment("c5_resin", 12) == -2.5
    assert compute_seasonal_adjustment("c5_resin", 6) == 1.0


# ── Causal-chain adjustment ─────────────────────────────────────────────────

def test_causal_chain_zero_when_no_naphtha_signal():
    """No naphtha % change → causal adjustment is 0.0."""
    assert compute_causal_chain_adjustment("c5_resin", None) == 0.0


def test_causal_chain_zero_for_unknown_product():
    """Unknown product → 0.0 even if naphtha moved."""
    assert compute_causal_chain_adjustment("unknown", 10.0) == 0.0


def test_causal_chain_propagates_naphtha_to_c5_resin():
    """Naphtha +10% → c5_resin moves +10 × 0.4464 = +4.464%."""
    pct = compute_causal_chain_adjustment("c5_resin", 10.0)
    assert pct == pytest.approx(4.464, abs=1e-3)


def test_causal_chain_propagates_negative_naphtha_move():
    """Naphtha -5% → c5_resin moves -5 × 0.4464 = -2.232%."""
    pct = compute_causal_chain_adjustment("c5_resin", -5.0)
    assert pct == pytest.approx(-2.232, abs=1e-3)


def test_causal_chain_naphtha_self_is_full_pass_through():
    """Naphtha elasticity = 1.0, so naphtha +8% → naphtha +8%."""
    pct = compute_causal_chain_adjustment("naphtha", 8.0)
    assert pct == pytest.approx(8.0)


# ── Combined domain signal adjustment ───────────────────────────────────────

def test_combined_adjustment_seasonal_only_when_no_naphtha():
    """Without a naphtha signal, only the seasonal rule applies."""
    result = compute_domain_signal_adjustment(
        product_id="c5_resin",
        as_of_date=datetime(2026, 12, 15),  # December → -2.5%
        naphtha_pct_change=None,
    )
    assert result["seasonal_pct"] == -2.5
    assert result["causal_pct"] == 0.0
    assert result["total_pct"] == -2.5
    assert "seasonal:c5_resin:12" in result["applied_rules"]
    assert not any(r.startswith("causal:") for r in result["applied_rules"])


def test_combined_adjustment_applies_both_layers():
    """Both seasonal + causal apply when naphtha signal is present."""
    result = compute_domain_signal_adjustment(
        product_id="c5_resin",
        as_of_date=datetime(2026, 6, 15),  # June → +1.0%
        naphtha_pct_change=10.0,           # → +4.464%
    )
    assert result["seasonal_pct"] == 1.0
    assert result["causal_pct"] == pytest.approx(4.464, abs=1e-3)
    assert result["total_pct"] == pytest.approx(5.464, abs=1e-3)
    assert "seasonal:c5_resin:6" in result["applied_rules"]
    assert "causal:c5_resin:naphtha=+10.0%" in result["applied_rules"]


def test_combined_adjustment_no_rules_for_unknown_product():
    """Unknown product → all zeros, empty applied_rules."""
    result = compute_domain_signal_adjustment(
        product_id="unknown",
        as_of_date=datetime(2026, 7, 1),
        naphtha_pct_change=10.0,
    )
    assert result["seasonal_pct"] == 0.0
    assert result["causal_pct"] == 0.0
    assert result["total_pct"] == 0.0
    assert result["applied_rules"] == []
