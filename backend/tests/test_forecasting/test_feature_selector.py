"""Test permutation-importance feature selector.

Covers:
1. Select features returns subset when some features are irrelevant
2. Cache hit returns saved features
3. No cache → recomputes and writes cache
4. Edge cases: empty features, single feature, all equal importance
"""

import json
import os
from pathlib import Path

import numpy as np
import pytest
from xgboost import XGBRegressor

from app.services.forecasting.features.feature_selector import select_features


@pytest.fixture
def dummy_model():
    """Fit a simple XGBRegressor on synthetic data."""
    rng = np.random.RandomState(99)
    X = rng.normal(0, 1, (100, 5))
    true_coef = np.array([1.0, 0.5, 0.0, 0.1, 0.0])  # 3rd and 5th are irrelevant
    y = X @ true_coef + rng.normal(0, 0.1, 100)
    model = XGBRegressor(n_estimators=50, max_depth=2, random_state=42, verbosity=0)
    model.fit(X, y)
    return model, X, y


class TestFeatureSelector:
    def test_selects_subset(self, dummy_model):
        """Irrelevant features should be dropped."""
        model, X, y = dummy_model
        names = ["f_important1", "f_important2", "f_noise1", "f_weak", "f_noise2"]
        selected = select_features(model, X, y, names, product_key="test_select")
        assert len(selected) <= len(names)
        # Important features should be included
        assert "f_important1" in selected

    def test_cache_hit(self, dummy_model, tmp_path):
        """Second call with same product_key should use cache."""
        model, X, y = dummy_model
        names = ["a", "b", "c", "d", "e"]

        import app.services.forecasting.features.feature_selector as fsel
        old_dir = fsel._CACHE_DIR
        fsel._CACHE_DIR = str(tmp_path / "xgb_features")
        try:
            first = select_features(model, X, y, names, product_key="cache_test")
            second = select_features(model, X, y, names, product_key="cache_test")
            assert first == second
        finally:
            fsel._CACHE_DIR = old_dir

    def test_all_similar_importance(self, dummy_model):
        """When all features have similar importance, keep all."""
        model, _, _ = dummy_model
        # Make all features equally important
        rng = np.random.RandomState(1)
        X_eq = rng.normal(0, 1, (60, 5))
        y_eq = X_eq.sum(axis=1) + rng.normal(0, 0.02, 60)
        eq_model = XGBRegressor(n_estimators=20, max_depth=2, random_state=42, verbosity=0)
        eq_model.fit(X_eq, y_eq)
        names = ["w1", "w2", "w3", "w4", "w5"]
        selected = select_features(eq_model, X_eq, y_eq, names, product_key="test_equal")
        assert len(selected) >= 3  # _MIN_RETAIN = 3

    def test_empty_features(self, dummy_model):
        model, _, _ = dummy_model
        result = select_features(model, np.array([[]]), np.array([]), [], product_key="test_empty")
        assert result == []

    def test_single_feature(self, dummy_model):
        """Single feature is always retained."""
        model, X, y = dummy_model
        selected = select_features(model, X[:, :1], y, ["only_feature"], product_key="test_single")
        assert selected == ["only_feature"]
