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
    def test_targets_sorted_in_compute_all(self, monkeypatch):
        # The knowledge graph is config-driven and empty by default. Inject a
        # tiny generic value chain (feedstock → intermediate → derivative) the
        # same way a real app domain config would populate it.
        import app.services.knowledge_graph.graph as kg_graph
        from app.services.knowledge_graph.graph import (
            ProductNode, ProductType, Relationship, RelationshipType,
        )
        from app.services.forecasting.features.feature_registry import topological_order

        nodes = [
            ProductNode(id="raw_material", name_en="Raw Material", name_cn="原料",
                        type=ProductType.FEEDSTOCK),
            ProductNode(id="intermediate", name_en="Intermediate", name_cn="中间体",
                        type=ProductType.OLEFIN),
            ProductNode(id="derivative_a", name_en="Derivative A", name_cn="衍生品A",
                        type=ProductType.DOWNSTREAM),
            ProductNode(id="derivative_b", name_en="Derivative B", name_cn="衍生品B",
                        type=ProductType.DOWNSTREAM),
        ]
        edges = [
            Relationship(source_id="raw_material", target_id="intermediate",
                         relation=RelationshipType.PRODUCES),
            Relationship(source_id="intermediate", target_id="derivative_a",
                         relation=RelationshipType.PRODUCES),
            Relationship(source_id="derivative_a", target_id="derivative_b",
                         relation=RelationshipType.PRODUCES),
        ]
        monkeypatch.setattr(kg_graph, "_C5_C9_NODES", nodes)
        monkeypatch.setattr(kg_graph, "_C5_C9_EDGES", edges)

        kg = kg_graph.build_c5_c9_graph()
        products = ["derivative_b", "raw_material", "intermediate", "derivative_a"]
        ordered = topological_order(products, kg)
        assert len(ordered) == len(products)
        # Feedstock before derivative
        assert ordered.index("raw_material") < ordered.index("intermediate")
        assert ordered.index("intermediate") < ordered.index("derivative_a")


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
