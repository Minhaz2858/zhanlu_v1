"""Tests for KG-driven feature spec derivation and topological ordering."""
from __future__ import annotations

import pytest

from app.services.knowledge_graph.graph import build_c5_c9_graph
from app.services.forecasting.features.feature_registry import (
    FeatureSpec, derive_feature_spec, topological_order,
)


class TestDeriveFeatureSpec:
    def test_upstream_product_has_empty_feedstock(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("crude_oil", graph)
        assert spec.product_key == "crude_oil"
        assert spec.feedstock_keys == []

    def test_downstream_product_has_feedstock_chain(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("isoprene", graph)
        assert "cracked_c5" in spec.feedstock_keys
        assert "naphtha" in spec.feedstock_keys
        assert "crude_oil" in spec.feedstock_keys

    def test_spread_pairs_built_from_adjacent_feedstocks(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("isoprene", graph)
        assert len(spec.spread_pairs) >= 1
        for pair in spec.spread_pairs:
            assert isinstance(pair, tuple)
            assert len(pair) == 2

    def test_use_fx_true_when_chain_includes_imported(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("isoprene", graph)
        assert spec.use_fx is True

    def test_use_fx_false_for_no_feedstocks(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("crude_oil", graph)
        assert spec.use_fx is False

    def test_unknown_product_returns_empty_spec(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("nonexistent_product", graph)
        assert spec.feedstock_keys == []

    def test_custom_feedstock_lags(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("isoprene", graph, feedstock_lags=[1, 7, 14])
        assert spec.feedstock_lags == [1, 7, 14]

    def test_default_feedstock_lags(self):
        graph = build_c5_c9_graph()
        spec = derive_feature_spec("isoprene", graph)
        assert spec.feedstock_lags == [1, 2, 3, 7]


class TestTopologicalOrder:
    def test_feedstocks_before_derivatives(self):
        graph = build_c5_c9_graph()
        products = ["isoprene", "crude_oil", "naphtha", "cracked_c5"]
        ordered = topological_order(products, graph)
        assert ordered.index("crude_oil") < ordered.index("naphtha")
        assert ordered.index("naphtha") < ordered.index("cracked_c5")
        assert ordered.index("cracked_c5") < ordered.index("isoprene")

    def test_all_products_returned(self):
        graph = build_c5_c9_graph()
        products = ["crude_oil", "naphtha", "isoprene", "dcpd"]
        ordered = topological_order(products, graph)
        assert set(ordered) == set(products)

    def test_products_not_in_graph_returned_at_end(self):
        graph = build_c5_c9_graph()
        products = ["crude_oil", "unknown_product"]
        ordered = topological_order(products, graph)
        assert ordered.index("crude_oil") < ordered.index("unknown_product")
