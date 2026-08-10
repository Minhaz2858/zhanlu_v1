"""Wave 4: Foundation Model Tests

Verifies that Chronos-Bolt and Moirai are correctly registered in the
model pool when the appropriate flags are ON.  The actual model inference
is tested only for the import path — full inference requires pre-downloaded
HF model weights.
"""

import numpy as np
import pandas as pd
import pytest


# ============================================================================
# 1. Model pool registration
# ============================================================================

class TestFoundationModelPool:
    def test_chronos_in_pool(self):
        from app.services.forecasting.models import build_model_pool

        y = pd.Series(
            np.random.normal(100, 5, 120),
            index=pd.date_range("2024-01-01", periods=120, freq="D"),
            name="price",
        )
        pool = build_model_pool(y=y)
        assert "chronos_bolt" in pool, "Chronos-Bolt must be in pool"
        assert pool["chronos_bolt"].name == "chronos_bolt"
        assert pool["chronos_bolt"].min_history == 60

    def test_moirai_in_pool(self):
        from app.services.forecasting.models import build_model_pool

        y = pd.Series(
            np.random.normal(100, 5, 120),
            index=pd.date_range("2024-01-01", periods=120, freq="D"),
            name="price",
        )
        pool = build_model_pool(y=y)
        assert "moirai" in pool, "Moirai must be in pool"
        assert pool["moirai"].name == "moirai"
        assert pool["moirai"].min_history == 60

    def test_foundation_models_not_in_pool_without_y(self):
        """Without y, foundation models are skipped (guard in build_model_pool)."""
        from app.services.forecasting.models import build_model_pool

        pool = build_model_pool()
        assert "chronos_bolt" not in pool
        assert "moirai" not in pool


# ============================================================================
# 2. Import sanity
# ============================================================================

class TestFoundationModelImports:
    def test_chronos_wrapper_importable(self):
        from app.services.forecasting.models.chronos_bolt import (
            ChronosBoltModel,
        )

        assert ChronosBoltModel.name == "chronos_bolt"

    def test_moirai_wrapper_importable(self):
        from app.services.forecasting.models.moirai import MoiraiModel

        assert MoiraiModel.name == "moirai"

    def test_chronos_lazy_load_does_not_crash_on_offline(self):
        """Constructor should NOT raise (deferred loading)."""
        from app.services.forecasting.models.chronos_bolt import (
            ChronosBoltModel,
        )

        m = ChronosBoltModel()
        assert m is not None
        assert m._pipeline is None  # not downloaded

    def test_moirai_lazy_load_does_not_crash_on_offline(self):
        """Constructor should NOT raise (deferred loading)."""
        from app.services.forecasting.models.moirai import MoiraiModel

        m = MoiraiModel()
        assert m is not None
        assert m._model is None  # not downloaded


# ============================================================================
# 3. Graceful degradation on fit (model unavailable)
# ============================================================================

class TestFoundationModelGracefulDegradation:
    def test_chronos_fit_without_model_raises(self):
        """fit() should raise when pipeline not downloaded."""
        from app.services.forecasting.models.chronos_bolt import (
            ChronosBoltModel,
        )

        m = ChronosBoltModel()
        y = pd.Series(
            np.random.normal(100, 5, 120),
            index=pd.date_range("2024-01-01", periods=120, freq="D"),
            name="price",
        )
        # First fit should trigger _load_pipeline → logs warning, returns None
        # Then _predict_samples raises ModelFitError because pipeline is None
        m.fit(y)
        with pytest.raises(Exception):
            m.forecast(h=7)
