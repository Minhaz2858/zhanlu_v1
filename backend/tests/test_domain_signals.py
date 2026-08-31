"""Tests for the config-driven domain-signal overlay (elasticities + seasonal rules).

Elasticities and seasonal rules are per-app domain-config data (the
``domain_signals`` block in a ``domain_configs/<agent>.json`` file). With no
config the overlay is a no-op — every adjustment is 0.0 and the platform stays
generic. These tests exercise the config-driven mechanism using a temporary
generic config injected through the ``domain_signals_config`` fixture
(see tests/conftest.py), which also writes the JSON file and points
ZHL_DOMAIN_CONFIG_DIR at it.
"""
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


# ── Empty config = fully generic platform (no data loaded) ─────────────────

def test_empty_config_tables_are_empty():
    """No domain config → no elasticities / seasonal rules loaded."""
    assert _ELASTICITIES == {}
    assert _RAW_ELASTICITIES == {}
    assert _SEASONAL_RULES == {}


def test_empty_config_compute_domain_signal_adjustment_all_zeros():
    """Empty config → combined adjustment is 0.0 for ANY product."""
    result = compute_domain_signal_adjustment(
        product_id="widget",
        as_of_date=datetime(2026, 12, 15),
        naphtha_pct_change=10.0,
    )
    assert result["seasonal_pct"] == 0.0
    assert result["causal_pct"] == 0.0
    assert result["total_pct"] == 0.0
    assert result["applied_rules"] == []


def test_empty_config_seasonal_adjustment_zero_for_any_product():
    """Empty config → seasonal adjustment is 0.0 for any (product, month)."""
    assert compute_seasonal_adjustment("widget", 12) == 0.0
    assert compute_seasonal_adjustment("gadget", 6) == 0.0


def test_empty_config_causal_adjustment_zero_for_any_product():
    """Empty config → causal-chain adjustment is 0.0 for any product."""
    assert compute_causal_chain_adjustment("widget", 10.0) == 0.0
    assert compute_causal_chain_adjustment("gadget", -5.0) == 0.0
    assert compute_causal_chain_adjustment("widget", None) == 0.0


def test_empty_config_no_elasticity_entries():
    """Empty config → no product has an effective elasticity."""
    for pid in ("widget", "gadget", "c5_resin", "naphtha"):
        assert _ELASTICITIES.get(pid) is None


# ── Config-driven data (injected via the domain_signals_config fixture) ────

def test_config_elasticity_tables_loaded(domain_signals_config):
    """Configured elasticities appear as raw × dampening, rounded to 4 dp."""
    assert _RAW_ELASTICITIES["widget"] == (0.5, 1.0)
    assert _ELASTICITIES["widget"] == 0.5
    assert _ELASTICITIES["gadget"] == round(0.8 * 0.85, 4)  # 0.68
    for pid, (raw, damp) in _RAW_ELASTICITIES.items():
        expected = round(raw * damp, 4)
        assert _ELASTICITIES[pid] == expected, (
            f"{pid}: {raw}×{damp}={expected} but _ELASTICITIES has "
            f"{_ELASTICITIES[pid]}"
        )


def test_config_seasonal_rules_loaded(domain_signals_config):
    """Configured "product|month" keys become (product, month) rule tuples."""
    assert _SEASONAL_RULES[("widget", 12)] == -2.5
    assert _SEASONAL_RULES[("widget", 6)] == 1.0


def test_config_compute_seasonal_adjustment_returns_configured_value(
    domain_signals_config,
):
    """Known (product, month) → the configured pct value."""
    assert compute_seasonal_adjustment("widget", 12) == -2.5
    assert compute_seasonal_adjustment("widget", 6) == 1.0


def test_config_compute_seasonal_adjustment_unlisted_month(domain_signals_config):
    """Product has rules, but no rule for this month → 0.0."""
    assert compute_seasonal_adjustment("widget", 7) == 0.0


def test_config_compute_causal_chain_adjustment(domain_signals_config):
    """Feedstock +10% → widget moves +10 × 0.5 = +5.0%."""
    pct = compute_causal_chain_adjustment("widget", 10.0)
    assert pct == pytest.approx(5.0)


def test_config_compute_causal_chain_negative(domain_signals_config):
    """Feedstock -5% → widget moves -5 × 0.5 = -2.5%."""
    pct = compute_causal_chain_adjustment("widget", -5.0)
    assert pct == pytest.approx(-2.5)


def test_config_compute_causal_chain_no_signal(domain_signals_config):
    """No feedstock signal → 0.0 even when the product has an elasticity."""
    assert compute_causal_chain_adjustment("widget", None) == 0.0


def test_config_unknown_product_returns_zero(domain_signals_config):
    """Unknown product → 0.0 even with a config loaded."""
    assert compute_seasonal_adjustment("unknown", 12) == 0.0
    assert compute_causal_chain_adjustment("unknown", 10.0) == 0.0


# ── Combined overlay ───────────────────────────────────────────────────────

def test_config_combined_seasonal_only_when_no_feedstock(domain_signals_config):
    """Without a feedstock signal, only the seasonal rule applies."""
    result = compute_domain_signal_adjustment(
        product_id="widget",
        as_of_date=datetime(2026, 12, 15),  # December → -2.5%
        naphtha_pct_change=None,
    )
    assert result["seasonal_pct"] == -2.5
    assert result["causal_pct"] == 0.0
    assert result["total_pct"] == -2.5
    assert "seasonal:widget:12" in result["applied_rules"]
    assert not any(r.startswith("causal:") for r in result["applied_rules"])


def test_config_combined_applies_both_layers(domain_signals_config):
    """Both seasonal + causal apply when a feedstock signal is present."""
    result = compute_domain_signal_adjustment(
        product_id="widget",
        as_of_date=datetime(2026, 6, 15),  # June → +1.0%
        naphtha_pct_change=10.0,           # → +5.0% causal
    )
    assert result["seasonal_pct"] == 1.0
    assert result["causal_pct"] == pytest.approx(5.0)
    assert result["total_pct"] == pytest.approx(6.0)
    assert "seasonal:widget:6" in result["applied_rules"]
    assert "causal:widget:feedstock=+10.0%" in result["applied_rules"]


def test_config_combined_unknown_product_all_zeros(domain_signals_config):
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


# ── Config-file loading mechanism ──────────────────────────────────────────

def test_config_file_loadable_via_env_dir(domain_signals_config):
    """The temporary JSON is genuinely loadable through ZHL_DOMAIN_CONFIG_DIR."""
    from app.services import domain_config as dc

    raw = dc._load_file_for("forecast")  # file written by the fixture
    assert raw["domain_signals"]["elasticities"]["widget"] == [0.5, 1.0]
    assert raw["domain_signals"]["seasonal_rules"]["widget|12"] == -2.5


# ── Feedstock signal fetcher ───────────────────────────────────────────────

def test_fetch_feedstock_returns_none_when_no_target():
    """No feedstock ForecastTarget → None (graceful degradation)."""
    from app.services.forecasting.domain_signals import (
        fetch_root_feedstock_pct_change,
    )

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    class _FakeDb:
        def query(self, model):
            return _FakeQuery()

    assert fetch_root_feedstock_pct_change(_FakeDb()) is None
