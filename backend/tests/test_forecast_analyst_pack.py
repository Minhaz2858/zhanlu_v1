"""Evidence pack builder — pure assembly."""
import pytest

from app.services.forecasting.analyst.evidence_pack import (
    UPSTREAM_MAP, build_pack, compute_price_percentile,
)

# The analyst pack's upstream map / elasticities / seasonal rules are
# config-driven. Run every test in this file against a temporary generic
# config ("widget" ← "gadget", etc.) injected by the shared fixture.
pytestmark = pytest.mark.usefixtures("domain_signals_config")


def _history(start=6000.0, n=100, step=1.0):
    return [(f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", start + i * step) for i in range(n)]


def _explanation():
    return {
        "drivers": [{"feature": "cracked_c5", "weight": 0.42},
                     {"feature": "fx", "weight": 0.2},
                     {"feature": "event", "weight": 0.1},
                     {"feature": "extra", "weight": 0.05}],
        "trust_tier": {"tier": "low", "reason_zh": "历史数据稀疏",
                        "reason_codes": ["sparse_data"], "badge_label_zh": "数据不足"},
        "probability": {"7": {"p_rise": 0.42, "expected_change_pct": -0.04}},
        "directional": {"7": {"accuracy": 0.51, "status": "no_edge", "n_test": 40}},
        "decision": {"7": {"action": "watch", "confidence": "low", "rationale": "Low trust"}},
        "model_agreement": {"7": {"n_models": 8, "min": 6580.0, "max": 6790.0, "spread_pct": 0.031}},
        "intelligence": {"active_event_count": 0, "bias_direction": "neutral", "summary": None},
        "policy": {"volatility_regime": "NORMAL"},
        "domain_signals": None,
    }


def _pack(**over):
    kw = dict(
        product_id="widget", name_zh="Widget", day=7,
        history_rows=_history(start=7000.0, step=-0.8),
        upstream_histories={"gadget": _history(start=5800.0, step=-0.5)},
        run_results={"7": {"base": [6660.0, 6650.0], "bull": [6900.0, 6950.0], "bear": [6400.0, 6350.0]}},
        model_detail={"models_run": ["a"] * 8, "ensemble_mape": 0.09, "naive_mape": 0.11},
        explanation=_explanation(),
        as_of_month=8,
    )
    kw.update(over)
    return build_pack(**kw)


def test_pack_core_numbers():
    p = _pack()
    assert p["forecast_base"] == 6660.0
    assert p["forecast_end"] == 6650.0
    assert p["bull"] == 6950.0 and p["bear"] == 6350.0  # last point of curves
    assert p["expected_change_pct"] == -0.04
    assert p["p_rise"] == 0.42
    assert p["decision"]["action"] == "watch"
    assert p["trust"]["reason_codes"] == ["sparse_data"]
    assert len(p["drivers"]) == 3  # top-3 only


def test_pack_causal_transmission():
    p = _pack()
    up = p["upstream"][0]
    assert up["product_id"] == "gadget"
    assert up["chg_30d_pct"] is not None and up["chg_30d_pct"] < 0
    # widget: configured raw 0.5 × damp 1.0 → elasticity 0.5
    assert p["causal"]["elasticity"] == 0.5
    assert p["causal"]["implied_pct"] < 0  # falling upstream → negative implied


def test_pack_divergence_flag():
    # upstream implies strong fall, model says rise → divergent
    p = _pack(explanation={**_explanation(),
                           "probability": {"7": {"p_rise": 0.6, "expected_change_pct": 0.05}}})
    assert p["causal"]["divergent"] is True
    p2 = _pack()  # both negative → not divergent
    assert p2["causal"]["divergent"] is False


def test_pack_seasonal_label():
    p = _pack()  # widget month 8 → +1.5 in rules
    assert p["seasonal"]["adj_pct"] == 1.5
    assert p["seasonal"]["label_zh"] == "传统需求旺季"
    p_winter = _pack(as_of_month=12)  # widget|12 → -2.5
    assert p_winter["seasonal"]["label_zh"] == "传统需求淡季"


def test_pack_agreement_from_explanation():
    p = _pack()
    assert p["models"]["agreement"]["spread_pct"] == 0.031
    assert p["models"]["model_count"] == 8


def test_pack_handles_empty_explanation():
    p = _pack(explanation={}, run_results={}, model_detail=None)
    assert p["decision"]["action"] == "watch"
    assert p["p_rise"] is None
    assert p["drivers"] == []
    assert p["models"]["agreement"] is None


def test_price_percentile():
    rows = _history(start=100.0, n=100, step=1.0)  # rising; last = highest
    assert compute_price_percentile(rows) == 99.0
    assert compute_price_percentile(rows[:5]) is None  # <10 points


def test_upstream_map_covers_configured_products():
    """The upstream map is config-driven; configured products are present."""
    assert "widget" in UPSTREAM_MAP
    assert UPSTREAM_MAP["widget"] == ["gadget"]


def test_pack_implied_change_pct():
    p = _pack()
    # current = 7000 - 0.8*99 = 6920.8 ; base = 6660
    assert p["implied_change_pct"] == round(6660.0 / 6920.8 - 1.0, 4)
    p_none = _pack(run_results={})
    assert p_none["implied_change_pct"] is None


def test_pack_product_group_classification():
    # widget is a downstream product (a key in the configured upstream map),
    # gadget is its upstream, unknown products are unclassified.
    assert _pack().get("product_group") == "downstream"
    assert _pack(product_id="gadget", name_zh="Gadget",
                 upstream_histories={"widget": _history(start=5000.0, step=1.0)}).get("product_group") == "upstream"
    assert _pack(product_id="unknown_xyz", name_zh="Unknown",
                 upstream_histories={}).get("product_group") == "unknown"


def test_pack_unknown_product_has_empty_upstream():
    p = _pack(product_id="unknown_xyz", name_zh="Unknown", upstream_histories={})
    assert p["upstream"] == []
    assert p["causal"]["elasticity"] is None
