"""Tests for KG-driven feature spec derivation and topological ordering."""
from __future__ import annotations

import pytest

from app.services.knowledge_graph.graph import build_sample_graph
from app.services.forecasting.features.feature_registry import (
    FeatureSpec, derive_feature_spec, topological_order,
)


class TestDeriveFeatureSpec:
    def test_upstream_product_has_empty_feedstock(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_e", graph)
        assert spec.product_key == "product_e"
        assert spec.feedstock_keys == []

    def test_downstream_product_has_feedstock_chain(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert "cracked_c5" in spec.feedstock_keys
        assert "product_c" in spec.feedstock_keys
        assert "product_e" in spec.feedstock_keys

    def test_spread_pairs_built_from_adjacent_feedstocks(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert len(spec.spread_pairs) >= 1
        for pair in spec.spread_pairs:
            assert isinstance(pair, tuple)
            assert len(pair) == 2

    def test_use_fx_true_when_chain_includes_imported(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert spec.use_fx is True

    def test_use_fx_false_for_no_feedstocks(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_e", graph)
        assert spec.use_fx is False

    def test_unknown_product_returns_empty_spec(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("nonexistent_product", graph)
        assert spec.feedstock_keys == []

    def test_custom_feedstock_lags(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph, feedstock_lags=[1, 7, 14])
        assert spec.feedstock_lags == [1, 7, 14]

    def test_default_feedstock_lags(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert spec.feedstock_lags == [1, 2, 3, 7]


class TestTopologicalOrder:
    def test_feedstocks_before_derivatives(self):
        graph = build_sample_graph()
        products = ["product_d", "product_e", "product_c", "cracked_c5"]
        ordered = topological_order(products, graph)
        assert ordered.index("product_e") < ordered.index("product_c")
        assert ordered.index("product_c") < ordered.index("cracked_c5")
        assert ordered.index("cracked_c5") < ordered.index("product_d")

    def test_all_products_returned(self):
        graph = build_sample_graph()
        products = ["product_e", "product_c", "product_d", "product_b"]
        ordered = topological_order(products, graph)
        assert set(ordered) == set(products)

    def test_products_not_in_graph_returned_at_end(self):
        graph = build_sample_graph()
        products = ["product_e", "unknown_product"]
        ordered = topological_order(products, graph)
        assert ordered.index("product_e") < ordered.index("unknown_product")
