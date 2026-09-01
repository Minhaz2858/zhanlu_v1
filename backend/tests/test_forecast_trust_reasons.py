"""forecast_trust_tier — machine-readable reason codes."""
from app.services.forecasting.forecast_trust_tier import compute_forecast_trust_tier


def test_low_tier_sparse_data_code():
    out = compute_forecast_trust_tier("dcpd", below_naive=False, cadence_class="sparse")
    assert out["tier"] == "low"
    assert "sparse_data" in out["reason_codes"]


def test_directional_below_naive_code():
    out = compute_forecast_trust_tier("isoprene", below_naive=True, cadence_class="daily")
    assert out["tier"] == "directional"
    assert "below_naive_baseline" in out["reason_codes"]


def test_directional_weekly_product_has_both_codes():
    out = compute_forecast_trust_tier("cracked_c9", below_naive=True, cadence_class="weekly")
    assert "below_naive_baseline" in out["reason_codes"]
    assert "weekly_cadence" in out["reason_codes"]


def test_high_tier_code():
    out = compute_forecast_trust_tier("isoprene", below_naive=False, cadence_class="daily", mape=8.0)
    assert out["tier"] == "high"
    assert out["reason_codes"] == ["model_skill_high"]


def test_medium_weekly_code():
    out = compute_forecast_trust_tier("raffinate_c5", below_naive=False, cadence_class="weekly")
    assert out["tier"] == "medium"
    assert "weekly_cadence" in out["reason_codes"]


def test_medium_generic_code():
    out = compute_forecast_trust_tier("styrene", below_naive=False, cadence_class="daily")
    assert out["tier"] == "medium"
    assert out["reason_codes"] == ["model_skill_medium"]
