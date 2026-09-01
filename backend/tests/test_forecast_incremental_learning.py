"""P3-1 tests: Incremental online learning — warm-start + model state cache."""
from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd

import xgboost as xgb


# Patch the cache dir before importing model_cache
_orig_cache = os.environ.get("MODEL_CACHE_DIR")
_tmp_cache = tempfile.mkdtemp(prefix="model_cache_test_")
os.environ["MODEL_CACHE_DIR"] = _tmp_cache


def _cleanup():
    import shutil
    if os.path.isdir(_tmp_cache):
        shutil.rmtree(_tmp_cache, ignore_errors=True)
    if _orig_cache:
        os.environ["MODEL_CACHE_DIR"] = _orig_cache
    else:
        os.environ.pop("MODEL_CACHE_DIR", None)


from app.services.forecasting.model_cache import save_state, load_state, invalidate_cache


def _make_toy_xgb():
    """Train a minimal XGBoost model on toy data."""
    rng = np.random.RandomState(42)
    X = rng.randn(50, 5)
    y = X[:, 0] * 2.0 + X[:, 1] * -1.0 + rng.randn(50) * 0.1
    model = xgb.XGBRegressor(n_estimators=10, max_depth=2, learning_rate=0.1)
    model.fit(X, y)
    return model


def test_save_and_load_roundtrip():
    """Saved model can be reloaded and produces same predictions."""
    model = _make_toy_xgb()
    rng = np.random.RandomState(99)
    X_test = rng.randn(5, 5)
    pred_before = model.predict(X_test)

    # Save
    path = save_state(model, "test_product", "xgboost_reg")
    assert os.path.isfile(path), f"Expected saved file at {path}"

    # Load
    loaded, meta = load_state("test_product", "xgboost_reg", xgb)
    assert loaded is not None, "Expected model to load"
    pred_after = loaded.predict(X_test)

    # Predictions should match
    np.testing.assert_array_almost_equal(pred_before, pred_after, decimal=5)


def test_load_nonexistent_returns_none():
    """Loading a product with no cache returns None."""
    model, meta = load_state("zzz_no_cache", "xgboost_reg", xgb)
    assert model is None
    assert meta == {}


def test_invalidate_removes_cache():
    """Invalidate removes cached files."""
    model = _make_toy_xgb()
    save_state(model, "test_invalidate", "xgboost_reg")

    invalidate_cache("test_invalidate", "xgboost_reg")
    model2, _ = load_state("test_invalidate", "xgboost_reg", xgb)
    assert model2 is None, "Cache should be removed after invalidate"


def test_save_with_metadata():
    """Metadata is persisted alongside the model."""
    model = _make_toy_xgb()
    save_state(model, "test_meta", "xgboost_reg", metadata={"n_obs": 500, "mape": 3.2})

    loaded, meta = load_state("test_meta", "xgboost_reg", xgb)
    assert meta.get("n_obs") == 500
    assert meta.get("mape") == 3.2
