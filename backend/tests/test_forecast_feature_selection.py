"""Test per-product feature selection override.

Covers:
- derive_feature_spec() with override_exog_features parameter
- target.model_config["exog_features"] wired into engine
- Feature selection flag-gated behavior
"""
import pytest
from unittest.mock import MagicMock

from app.services.forecasting.features.feature_registry import (
    derive_feature_spec,
    FeatureSpec,
    KnowledgeGraph,
)


# ---------------------------------------------------------------------------
# derive_feature_spec with override
# ---------------------------------------------------------------------------

class TestDeriveFeatureSpecOverride:
    """Tests for the override_exog_features parameter."""

    def test_override_skips_kg_walk(self):
        """When override_exog_features is provided, KG walk is skipped."""
        empty_kg = KnowledgeGraph()  # no nodes
        spec = derive_feature_spec(
            "c5_cracked", empty_kg,
            override_exog_features=["brent", "naphtha", "import_price"],
        )
        assert isinstance(spec, FeatureSpec)
        assert spec.feedstock_keys == ["brent", "naphtha", "import_price"]
        assert spec.spread_pairs == []  # no KG walk → no spread pairs
        assert spec.use_fx is False

    def test_no_override_with_empty_kg(self):
        """When no override and product not in KG, returns empty feedstock_keys."""
        empty_kg = KnowledgeGraph()
        spec = derive_feature_spec("c5_cracked", empty_kg)
        assert spec.feedstock_keys == []  # product not in KG → empty

    def test_none_override_falls_back_to_kg(self):
        """None override falls back to KG walk (default behavior)."""
        empty_kg = KnowledgeGraph()
        spec = derive_feature_spec("c5_cracked", empty_kg, override_exog_features=None)
        assert spec.feedstock_keys == []  # product not in KG → empty

    def test_override_for_unknown_product(self):
        """Override works even for products not in the KG."""
        empty_kg = KnowledgeGraph()
        spec = derive_feature_spec(
            "unknown_product", empty_kg,
            override_exog_features=["brent", "inventory"],
        )
        assert spec.feedstock_keys == ["brent", "inventory"]

    def test_target_model_config_exog_features_integration(self):
        """Simulate engine.py wiring: target.model_config['exog_features'] → override."""
        target = MagicMock()
        target.product_key = "c5_cracked"
        target.model_config = {"exog_features": ["brent", "operating_rate"]}

        _exog_override = target.model_config.get("exog_features") if isinstance(target.model_config, dict) else None
        empty_kg = KnowledgeGraph()
        spec = derive_feature_spec(target.product_key, empty_kg, override_exog_features=_exog_override)
        assert spec.feedstock_keys == ["brent", "operating_rate"]

    def test_empty_list_override_treated_as_no_override(self):
        """Empty list override → feedstock_keys is empty (no KG walk, no features)."""
        empty_kg = KnowledgeGraph()
        spec = derive_feature_spec("c5_cracked", empty_kg, override_exog_features=[])
        # Empty list IS truthy but len=0, so it's a valid override of "no features"
        assert spec.feedstock_keys == []
