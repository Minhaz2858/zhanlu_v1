"""Model agreement metric + elasticity accessor."""
import pandas as pd

from app.services.forecasting.analyst.evidence_pack import compute_model_agreement
from app.services.forecasting.domain_signals import get_elasticity


def test_get_elasticity_configured_product(domain_signals_config):
    """Configured product → effective elasticity from the domain config."""
    assert get_elasticity("widget") == 0.5


def test_get_elasticity_unknown():
    assert get_elasticity("not_a_product") is None


def test_agreement_tight_cluster():
    fc = {
        "arima": pd.Series([100.0, 101.0]),
        "ets": pd.Series([100.5, 101.5]),
        "stl": pd.Series([99.5, 100.8]),
    }
    out = compute_model_agreement(fc, 2)
    assert out["n_models"] == 3
    assert out["min"] == 100.8
    assert out["max"] == 101.5
    assert out["spread_pct"] < 0.01


def test_agreement_wide_spread():
    fc = {"a": pd.Series([100.0]), "b": pd.Series([120.0])}
    out = compute_model_agreement(fc, 1)
    assert out["spread_pct"] == round(20.0 / 110.0, 4)


def test_agreement_single_model_returns_none():
    assert compute_model_agreement({"a": pd.Series([100.0])}, 1) is None


def test_agreement_short_series_skipped():
    fc = {"a": pd.Series([100.0, 101.0]), "b": pd.Series([50.0])}  # b too short for h=2
    assert compute_model_agreement(fc, 2) is None
