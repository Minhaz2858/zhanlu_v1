"""Tests for the market profile + coverage_dimensions derivation.

Covers:
  - Profile registry: ``get_profile("market")`` returns the 8-dimension profile.
  - Coverage signal: ``synthesize_market_report`` always emits
    ``coverage_dimensions`` reflecting which dimensions had successful facets.
  - Prefix-matching: a slightly variant facet_id (``core_metrics_brent``) still
    resolves to its parent dimension.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.enterprise_orchestrator.profiles import (
    Profile,
    get_profile,
    list_available_profiles,
    resolve_dimension,
)


# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------
class TestProfileRegistry:
    def test_market_profile_registered(self):
        profiles = list_available_profiles()
        assert "market" in profiles
        assert "enterprise" in profiles

    def test_get_market_profile(self):
        p = get_profile("market")
        assert isinstance(p, Profile)
        assert p.name == "market"
        assert p.label

    def test_market_profile_has_eight_dimensions(self):
        p = get_profile("market")
        # 8 mandatory market dimensions from the institutional-grade spec.
        assert len(p.facet_spec) == 8
        expected = {
            "core_metrics", "historical_trends", "cost_structure",
            "supply_side", "demand_side", "macro_context",
            "forward_indicators", "cross_segment_relationships",
        }
        assert set(p.facet_spec) == expected

    def test_market_profile_section_schema(self):
        p = get_profile("market")
        # 4 sections: overview_dashboard, executive_summary, entity_deep_dive, disclaimer
        assert "executive_summary" in p.section_schema
        assert "overview_dashboard" in p.section_schema
        assert "entity_deep_dive" in p.section_schema
        assert "disclaimer" in p.section_schema

    def test_unknown_profile_raises(self):
        from app.services.enterprise_orchestrator.profiles import ProfileNotFoundError
        with pytest.raises(ProfileNotFoundError):
            get_profile("nonexistent_profile_xyz")

    def test_get_profile_caches(self):
        # Calling twice should hit the cache (no error, returns same object).
        a = get_profile("market")
        b = get_profile("market")
        assert a is b


# ---------------------------------------------------------------------------
# Dimension resolution (prefix-matching)
# ---------------------------------------------------------------------------
class TestResolveDimension:
    def setup_method(self):
        self.p = get_profile("market")

    def test_exact_match(self):
        assert resolve_dimension(self.p, "core_metrics") == "core_metrics"
        assert resolve_dimension(self.p, "supply_side") == "supply_side"
        assert resolve_dimension(self.p, "forward_indicators") == "forward_indicators"

    def test_prefix_match(self):
        # LLM might emit slightly variant ids; resolve_dimension should
        # still find the parent dimension via prefix matching.
        assert resolve_dimension(self.p, "core_metrics_brent") == "core_metrics"
        assert resolve_dimension(self.p, "supply_side_refinery") == "supply_side"
        assert resolve_dimension(self.p, "forward_indicators_oil") == "forward_indicators"

    def test_unknown_returns_none(self):
        assert resolve_dimension(self.p, "totally_unknown_xyz") is None

    def test_empty_returns_none(self):
        assert resolve_dimension(self.p, "") is None
        assert resolve_dimension(self.p, None) is None


# ---------------------------------------------------------------------------
# Coverage derivation (the synthesizer helper)
# ---------------------------------------------------------------------------
class TestCoverageDerivation:
    """Verify ``_coverage_dimensions_for`` correctly rolls up facet results
    to coverage_dimensions via the market profile's facet_to_dimension map.
    """
    def test_all_eight_dimensions_covered(self):
        from app.services.enterprise_orchestrator.synthesizer import (
            _coverage_dimensions_for,
        )
        intent = {"profile_name": "market"}
        # 8 facets, one per dimension, all available with rows
        facets = {
            "core_metrics":                 {"available": True, "rows": [{}]},
            "historical_trends":            {"available": True, "rows": [{}]},
            "cost_structure":               {"available": True, "rows": [{}]},
            "supply_side":                  {"available": True, "rows": [{}]},
            "demand_side":                  {"available": True, "rows": [{}]},
            "macro_context":                {"available": True, "rows": [{}]},
            "forward_indicators":           {"available": True, "rows": [{}]},
            "cross_segment_relationships":  {"available": True, "rows": [{}]},
        }
        result = _coverage_dimensions_for(intent, facets)
        assert len(result) == 8
        assert set(result) == {
            "core_metrics", "historical_trends", "cost_structure",
            "supply_side", "demand_side", "macro_context",
            "forward_indicators", "cross_segment_relationships",
        }

    def test_unavailable_facets_excluded(self):
        from app.services.enterprise_orchestrator.synthesizer import (
            _coverage_dimensions_for,
        )
        intent = {"profile_name": "market"}
        # Only 3 of 8 facets available
        facets = {
            "core_metrics":         {"available": True,  "rows": [{}]},
            "historical_trends":    {"available": True,  "rows": [{}]},
            "cost_structure":       {"available": True,  "rows": [{}]},
            "supply_side":          {"available": False, "rows": []},
            "demand_side":          {"available": False, "rows": []},
            "macro_context":        {"available": False, "rows": []},
            "forward_indicators":   {"available": False, "rows": []},
            "cross_segment_relationships": {"available": False, "rows": []},
        }
        result = _coverage_dimensions_for(intent, facets)
        assert len(result) == 3
        assert "core_metrics" in result
        assert "supply_side" not in result

    def test_empty_facets_returns_empty(self):
        from app.services.enterprise_orchestrator.synthesizer import (
            _coverage_dimensions_for,
        )
        intent = {"profile_name": "market"}
        assert _coverage_dimensions_for(intent, {}) == []
        assert _coverage_dimensions_for(intent, None or {}) == []

    def test_empty_rows_treated_as_unavailable(self):
        """A facet that says available=True but has no rows must NOT count."""
        from app.services.enterprise_orchestrator.synthesizer import (
            _coverage_dimensions_for,
        )
        intent = {"profile_name": "market"}
        facets = {
            "core_metrics":  {"available": True, "rows": []},
            "supply_side":   {"available": True, "rows": [{}, {}]},
        }
        result = _coverage_dimensions_for(intent, facets)
        assert "core_metrics" not in result
        assert "supply_side" in result


# ---------------------------------------------------------------------------
# synthesize_market_report end-to-end (deterministic, no LLM)
# ---------------------------------------------------------------------------
class TestSynthesizeMarketReport:
    def test_emits_coverage_dimensions(self):
        from app.services.enterprise_orchestrator.synthesizer import (
            synthesize_market_report,
        )
        intent = {
            "profile_name": "market",
            "domain": "energy",
            "period": ("2026-08-01", "2026-08-25"),
            "primary_metric": "avg_price_brent",
            "segments": [],
            "facets": [],
        }
        facets = {
            f: {"available": True, "rows": [{"v": 1}], "summary": "x"}
            for f in [
                "core_metrics", "historical_trends", "cost_structure",
                "supply_side", "demand_side",
            ]
        }
        payload = synthesize_market_report(intent, facets)
        assert payload["enterprise_report_kind"] == "market_overview"
        assert "coverage_dimensions" in payload
        assert len(payload["coverage_dimensions"]) == 5
        assert "disclaimer" in payload
        assert "overview_dashboard" in payload
        assert "entity_deep_dive" in payload
