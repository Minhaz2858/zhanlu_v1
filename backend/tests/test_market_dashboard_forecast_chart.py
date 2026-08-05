"""
Tests for horizon-aware run selection and nearest-horizon slicing
in market_dashboard_service._pick_horizon_payload and get_forecast_chart.
"""

import pytest
from app.services.market_dashboard_service import (
    _pick_horizon_payload,
    _slice_curve,
    _curve_to_float_list,
)


class TestPickHorizonPayload:
    """_pick_horizon_payload returns (payload, effective_horizon) or ({}, None)."""

    def test_exact_key_with_d_suffix(self):
        """Prefer f"{days}d" exact match."""
        results = {"30d": {"base": [1, 2, 3]}, "7d": {"base": [4, 5]}}
        payload, horizon = _pick_horizon_payload(results, 30)
        assert horizon == 30
        assert payload == {"base": [1, 2, 3]}

    def test_exact_key_string_days(self):
        """Fall back to str(days) if f"{days}d" is missing."""
        results = {"30": {"base": [1, 2, 3]}}
        payload, horizon = _pick_horizon_payload(results, 30)
        assert horizon == 30
        assert payload == {"base": [1, 2, 3]}

    def test_exact_key_prefers_d_suffix_over_string(self):
        """f"{days}d" takes priority over str(days) when both exist."""
        results = {
            "30d": {"base": [1, 2, 3]},
            "30": {"base": [99, 99]},
        }
        payload, horizon = _pick_horizon_payload(results, 30)
        assert horizon == 30
        assert payload == {"base": [1, 2, 3]}

    def test_nearest_horizon_fallback_30_to_15(self):
        """When no 30d/30 key, fall back to nearest available (15)."""
        results = {"15d": {"base": [1, 2, 3], "bull": [4, 5, 6]}}
        payload, horizon = _pick_horizon_payload(results, 30)
        assert horizon == 15
        assert payload == {"base": [1, 2, 3], "bull": [4, 5, 6]}

    def test_nearest_horizon_fallback_15_to_30(self):
        """When no 15d/15 key, fall back to nearest available (30)."""
        results = {"30d": {"bear": [7, 8, 9]}}
        payload, horizon = _pick_horizon_payload(results, 15)
        assert horizon == 30
        assert payload == {"bear": [7, 8, 9]}

    def test_nearest_horizon_fallback_ties_prefer_larger(self):
        """Equidistant keys (e.g. 7 and 30 both 8 away from 19) → larger wins."""
        results = {"7d": {"base": [1]}, "30d": {"base": [2]}, "15d": {"base": [3]}}
        payload, horizon = _pick_horizon_payload(results, 19)
        # 7d=12 away, 15d=4 away, 30d=11 away — closest is 15
        assert horizon == 15
        assert payload == {"base": [3]}

    def test_empty_results_returns_empty(self):
        """Empty or None results → ({}, None)."""
        payload, horizon = _pick_horizon_payload({}, 7)
        assert payload == {}
        assert horizon is None

        payload, horizon = _pick_horizon_payload(None, 7)  # type: ignore
        assert payload == {}
        assert horizon is None

    def test_no_numeric_keys(self):
        """No numeric horizon keys at all → ({}, None)."""
        results = {"metadata": {"version": 1}, "status": "ok"}
        payload, horizon = _pick_horizon_payload(results, 7)
        assert payload == {}
        assert horizon is None

    def test_keys_with_various_formats(self):
        """Mixed string/numeric horizon keys are parsed correctly."""
        results = {
            "3d": {"base": [1]},
            "7": {"base": [2]},
            "15d": {"base": [3]},
            "irrelevant": {"base": [99]},
        }
        payload, horizon = _pick_horizon_payload(results, 15)
        assert horizon == 15
        assert payload == {"base": [3]}

    def test_payload_not_a_dict(self):
        """Non-dict value under the best horizon key → ({}, None).
        (Degenerate edge case; production results always have dict values.)"""
        results = {"7d": "not a dict", "30d": {"base": [1, 2, 3]}}
        payload, horizon = _pick_horizon_payload(results, 7)
        # 7d exists but is not a dict → no usable payload for requested 7
        assert payload == {}
        assert horizon is None


class TestSliceCurve:
    """_slice_curve slices curve arrays to `days` points."""

    def test_slice_longer_curve(self):
        curve = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = _slice_curve(curve, 3)
        assert result == [1.0, 2.0, 3.0]

    def test_slice_equal_length(self):
        curve = [1.0, 2.0, 3.0]
        result = _slice_curve(curve, 3)
        assert result == [1.0, 2.0, 3.0]

    def test_slice_shorter_curve(self):
        curve = [1.0, 2.0]
        result = _slice_curve(curve, 5)
        assert result == [1.0, 2.0]

    def test_empty_list(self):
        result = _slice_curve([], 3)
        assert result is None

    def test_non_list(self):
        result = _slice_curve("not a list", 3)  # type: ignore
        assert result is None

    def test_int_list_to_float(self):
        curve = [1, 2, 3, 4, 5]
        result = _slice_curve(curve, 3)
        assert result == [1, 2, 3]
        assert all(isinstance(v, int) for v in result)


class TestCurveToFloatList:
    """_curve_to_float_list normalises curves."""

    def test_list_of_floats(self):
        result = _curve_to_float_list([1.0, 2.0, 3.0])
        assert result == [1.0, 2.0, 3.0]

    def test_scalar_float(self):
        result = _curve_to_float_list(42.0)
        assert result == [42.0]

    def test_scalar_int(self):
        result = _curve_to_float_list(42)
        assert result == [42.0]

    def test_empty_list(self):
        result = _curve_to_float_list([])
        assert result is None

    def test_none(self):
        result = _curve_to_float_list(None)
        assert result is None
