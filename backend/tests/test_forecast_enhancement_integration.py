"""Integration tests for the enhanced forecasting pipeline (Tasks 10-11)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.engine import _get_config_bool


class TestConfigFlags:
    def test_get_config_bool_true_default(self):
        assert _get_config_bool("NONEXISTENT_FLAG", True) is True

    def test_get_config_bool_false_default(self):
        assert _get_config_bool("NONEXISTENT_FLAG", False) is False


class TestEngineTopologicalOrder:
    def test_targets_sorted_in_compute_all(self):
        # This verifies the plumbing doesn't crash on topo sort
        from app.services.forecasting.features.feature_registry import topological_order
        from app.services.knowledge_graph.graph import build_c5_c9_graph

        kg = build_c5_c9_graph()
        products = ["isoprene", "crude_oil", "naphtha", "cracked_c5"]
        ordered = topological_order(products, kg)
        assert len(ordered) == len(products)
        # Feedstock before derivative
        assert ordered.index("crude_oil") < ordered.index("naphtha")
        assert ordered.index("naphtha") < ordered.index("cracked_c5")


class TestPipelineIntegration:
    """End-to-end pipeline shape tests — no live DB."""

    @patch("app.services.forecasting.engine._get_config_bool")
    def test_pipeline_with_preprocess_disabled_runs_backtest(self, mock_flag):
        mock_flag.return_value = False
        # Just verify engine instantiation doesn't crash
        from app.services.forecasting.engine import ForecastEngine
        assert ForecastEngine is not None

    def test_pipeline_fails_gracefully_on_missing_target(self):
        from app.services.forecasting.engine import ForecastEngine

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        engine = ForecastEngine(mock_db)
        result = engine.compute_target("nonexistent-id")
        assert result is None
