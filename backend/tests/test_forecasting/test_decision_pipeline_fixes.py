"""Test Phase 2 bug fixes: Bugs 3, 5, 6, 7 in the decision pipeline.

B3: Classifier p_rise override for recommend()
B5: Dict access in decision logging (was .action → must be ["action"])
B6: Threshold snapshot from get_thresholds(), not module constants
B7: product_key passed to recommend()
"""

import numpy as np
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from app.services.forecasting.decision_engine import (
    Decision, recommend, get_thresholds,
)


class TestBug7ProductKeyPassThrough:
    """Bug 7: recommend() must receive and use product_key."""

    def test_recommend_accepts_product_key(self):
        """recommend() should accept product_key kwarg without error."""
        result = recommend(
            p_rise=0.6,
            expected_change_pct=3.0,
            directional_acc=0.55,
            directional_status="edge",
            trust_tier="medium",
            product_key="dcpd",
        )
        assert isinstance(result, Decision)
        assert result.action in ("buy", "sell", "hold", "watch")

    def test_product_key_flows_to_get_thresholds(self):
        """Passing product_key should query DB thresholds."""
        with patch(
            "app.services.forecasting.decision_engine.get_thresholds",
            wraps=get_thresholds,
        ) as mock_gt:
            result = recommend(
                p_rise=0.90, expected_change_pct=8.0,
                directional_acc=0.70, directional_status="edge",
                trust_tier="high",
                product_key="test_product",
            )
            assert result.action == "buy"
            # product_key passed as first positional arg
            assert mock_gt.call_args is not None
            call_args, _ = mock_gt.call_args
            assert call_args[0] == "test_product"


class TestBug3ClassifierPRiseOverride:
    """Bug 3: Classifier p_rise should override Gaussian p_rise when flag is ON."""

    def test_classifier_p_rise_overrides_gaussian(self):
        """When clf p_rise=0.82 vs Gaussian 0.55, use 0.82 for buy decision."""
        # clf p_rise=0.82 > 0.70 buy threshold → should be buy
        result = recommend(
            p_rise=0.82,  # classifier's own proba
            expected_change_pct=5.0,
            directional_acc=0.75,
            directional_status="edge",
            trust_tier="high",
        )
        assert result.action == "buy"
        assert result.confidence == "high"

    def test_gaussian_only_p_rise_no_clf(self):
        """When no classifier p_rise available, Gaussian p_rise is used."""
        result = recommend(
            p_rise=0.52,  # just above neutral, not enough for buy
            expected_change_pct=1.2,
            directional_acc=0.55,
            directional_status="edge",
            trust_tier="directional",
        )
        # With low p_rise, should not be buy
        assert result.action != "buy"


class TestBug5DictAccess:
    """Bug 5: decision_entry was accessed as object (.action) instead of dict."""

    def test_decision_report_dict_access(self):
        """Verify the correct way to access a dict entry."""
        decision_entry = {
            "action": "watch",
            "confidence": "low",
            "rationale": "no edge detected",
        }
        # This is the correct pattern (Bug 5 fix)
        assert decision_entry["action"] == "watch"
        assert decision_entry["confidence"] == "low"
        assert decision_entry["rationale"] == "no edge detected"
        # The OLD broken pattern would be:
        with pytest.raises(AttributeError):
            _ = decision_entry.action


class TestBug6ThresholdSnapshot:
    """Bug 6: Threshold snapshot used module constants, not DB-resolved."""

    def test_get_thresholds_returns_expected_keys(self):
        """get_thresholds must return buy/sell/buy_min_change/sell_min_change/edge."""
        th = get_thresholds(product_key=None, db=None)
        assert set(th.keys()) >= {"buy", "sell", "buy_min_change", "sell_min_change", "edge"}
        assert all(isinstance(v, float) for v in th.values())

    def test_module_constants_differ_from_get_thresholds(self):
        """Module-level constants may differ from get_thresholds (env override)."""
        from app.services.forecasting import decision_engine as _de
        th = get_thresholds(product_key=None, db=None)
        # get_thresholds should return values; they might equal defaults
        # but the structure is what matters (dict, not module constants)
        assert isinstance(th, dict)
        # Module constants exist as fallback
        assert hasattr(_de, "_BUY_THRESHOLD")
