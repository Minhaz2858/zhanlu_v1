"""Tests for synthesizer (Phase 1B).

Design spec §9 — pure-Python deterministic transforms; NO LLM call
involved. The synthesizer takes executed facets and assembles an
EnterpriseReport payload.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.enterprise_orchestrator.synthesizer import (
    synthesize_enterprise_report,
    _rank_rows,
    _top_share,
    _concentration,
    _margin_compression,
    _validate_enterprise_payload,
    RECOMMENDED_ACTION_RULES,
)


def _facet(facet_id, rows, summary="", source_sql="SELECT 1", purpose="primary"):
    return {
        "facet_id": facet_id,
        "kind": "service_call",
        "purpose": purpose,
        "rows": rows,
        "summary": summary,
        "source_sql": source_sql,
        "source_label": "erp_v_sale_orderentry",
        "row_count": len(rows),
        "warnings": [],
        "available": True,
        "unavailable_reason": "",
        "execution_log": [],
    }


def _intent(domain="supply_chain", facets=None, primary="total_revenue"):
    return {
        "domain": domain,
        "period": ("2026-07-25", "2026-08-23"),
        "primary_metric": primary,
        "segments": [],
        "facets": facets or [],
    }


# ---------------------------------------------------------------------------
# Pure-helper transforms
# ---------------------------------------------------------------------------
class TestRankRows:
    def test_descending(self):
        rows = [{"v": 1}, {"v": 5}, {"v": 3}]
        out = _rank_rows(rows, "v", descending=True)
        assert [r["v"] for r in out] == [5, 3, 1]

    def test_ascending(self):
        rows = [{"v": 1}, {"v": 5}, {"v": 3}]
        out = _rank_rows(rows, "v", descending=False)
        assert [r["v"] for r in out] == [1, 3, 5]

    def test_adds_rank_field(self):
        rows = [{"v": 5}, {"v": 1}]
        out = _rank_rows(rows, "v", descending=True)
        assert out[0]["__rank"] == 1
        assert out[1]["__rank"] == 2

    def test_missing_sort_key_unchanged(self):
        rows = [{"a": 1}, {"a": 2}]
        out = _rank_rows(rows, "missing", descending=True)
        assert out == rows


class TestTopShare:
    def test_topn_share(self):
        rows = [{"share_pct": 30}, {"share_pct": 20}, {"share_pct": 10}]
        assert _top_share(rows, n=2) == 50

    def test_too_few_rows_returns_zero(self):
        assert _top_share([], n=3) == 0
        assert _top_share([{"share_pct": 100}], n=3) == 100

    def test_missing_key_treated_as_zero(self):
        assert _top_share([{}, {"share_pct": 40}], n=3) == 40


class TestConcentration:
    def test_top3_concentration(self):
        rows = [
            {"customer_name": "A", "total_revenue": 100},
            {"customer_name": "B", "total_revenue": 50},
            {"customer_name": "C", "total_revenue": 30},
            {"customer_name": "D", "total_revenue": 20},
        ]
        # total = 200; top-3 = 100+50+30 = 180 → 90.0%
        assert _concentration(rows, "total_revenue") == 90.0

    def test_empty_returns_zero(self):
        assert _concentration([], "total_revenue") == 0.0


# ---------------------------------------------------------------------------
# Recommended-action rules
# ---------------------------------------------------------------------------
class TestRecommendedActions:
    def test_rules_table_non_empty(self):
        assert len(RECOMMENDED_ACTION_RULES) >= 3

    def test_low_stock_rule_fires(self):
        from app.services.enterprise_orchestrator.synthesizer import (
            _evaluate_recommended_actions,
        )
        facets = {
            "inventory_position": _facet(
                "inventory_position",
                rows=[{"material_name": "X", "days_of_stock": 3}],
                purpose="auxiliary",
            ),
        }
        actions = _evaluate_recommended_actions(facets)
        assert any("Restock" in a["action"] for a in actions)

    def test_concentration_rule_fires_above_60_pct(self):
        from app.services.enterprise_orchestrator.synthesizer import (
            _evaluate_recommended_actions,
        )
        rows = [
            {"customer_name": "A", "share_pct": 45},
            {"customer_name": "B", "share_pct": 25},
            {"customer_name": "C", "share_pct": 10},
            {"customer_name": "D", "share_pct": 5},
        ]
        facets = {
            "top_customers": _facet(
                "top_customers", rows=rows, purpose="auxiliary",
            ),
        }
        actions = _evaluate_recommended_actions(facets)
        assert any("Diversify" in a["action"] for a in actions)

    def test_no_firing_when_conditions_unmet(self):
        from app.services.enterprise_orchestrator.synthesizer import (
            _evaluate_recommended_actions,
        )
        facets = {
            "inventory_position": _facet(
                "inventory_position",
                rows=[{"material_name": "X", "days_of_stock": 30}],
                purpose="auxiliary",
            ),
        }
        actions = _evaluate_recommended_actions(facets)
        assert not any("Restock" in a["action"] for a in actions)


# ---------------------------------------------------------------------------
# Payload assembly + validation
# ---------------------------------------------------------------------------
class TestSynthesizeEnterpriseReport:
    def test_returns_6_sections(self):
        facets = {
            "sales_summary": _facet(
                "sales_summary",
                rows=[{"material_name": "X", "total_volume_tons": 100,
                       "total_revenue": 1000, "orders": 5}],
                purpose="primary",
            ),
            "inventory_position": _facet(
                "inventory_position",
                rows=[{"material_name": "X", "inventory_tons": 50,
                       "days_of_stock": 15}],
                purpose="auxiliary",
            ),
            "top_customers": _facet(
                "top_customers",
                rows=[{"customer_name": "A", "total_revenue": 1000,
                       "share_pct": 100, "orders": 5}],
                purpose="auxiliary",
            ),
        }
        intent = _intent(facets=[{"facet_id": k, "purpose": v["purpose"]}
                                   for k, v in facets.items()])
        intent["domain"] = "supply_chain"
        payload = synthesize_enterprise_report(intent, facets)
        assert payload["enterprise_report_kind"] == "executive"
        assert payload["executive_summary"]
        assert payload["primary_metric_breakdown"]["rows"]
        assert payload["segment_decomposition"]["rows"] or payload["segment_decomposition"]["observations"]
        assert payload["operational_drivers"]
        # The risk-section label is domain-adaptive.
        assert payload["risk_section"]["label"]
        assert payload["recommended_actions"] is not None
        assert payload["data_confidence"]

    def test_missing_facet_renders_unavailable(self):
        facets = {
            "sales_summary": _facet("sales_summary", rows=[{"a": 1}]),
        }
        intent = _intent(domain="supply_chain",
                         facets=[{"facet_id": "sales_summary", "purpose": "primary"}])
        payload = synthesize_enterprise_report(intent, facets)
        # inventory/top_customers/etc. absent → covered_facets has only 1
        assert set(payload["data_confidence"]["covered_facets"]) == {"sales_summary"}

    def test_period_label_in_title(self):
        facets = {"sales_summary": _facet("sales_summary", rows=[{"a": 1}])}
        intent = _intent(facets=[{"facet_id": "sales_summary", "purpose": "primary"}])
        payload = synthesize_enterprise_report(intent, facets)
        assert payload["period_label"] == "2026-07-25 to 2026-08-23"


class TestValidateEnterprisePayload:
    def test_rejects_too_few_primary_rows(self):
        bad = {
            "primary_metric_breakdown": {"rows": [{"a": 1}]},
            "executive_summary": "",
        }
        ok, why = _validate_enterprise_payload(bad)
        assert ok is False

    def test_rejects_no_executive_summary(self):
        bad = {
            "primary_metric_breakdown": {"rows": [{"a": 1}, {"b": 2}]},
            "executive_summary": "",
        }
        ok, why = _validate_enterprise_payload(bad)
        assert ok is False

    def test_accepts_minimum_valid(self):
        good = {
            "primary_metric_breakdown": {"rows": [{"a": 1}, {"b": 2}]},
            "executive_summary": "summary text",
        }
        ok, why = _validate_enterprise_payload(good)
        assert ok is True


# ---------------------------------------------------------------------------
# Margin-compression helper
# ---------------------------------------------------------------------------
class TestMarginCompression:
    def test_detects_drop(self):
        primary = {"rows": [{"margin_pct": 18.0}, {"margin_pct": 20.0}]}
        prior = {"rows": [{"margin_pct": 22.0}, {"margin_pct": 24.0}]}
        delta, segment = _margin_compression(primary, prior)
        assert delta and delta < 0

    def test_no_drop_returns_none(self):
        primary = {"rows": [{"margin_pct": 25.0}, {"margin_pct": 26.0}]}
        prior = {"rows": [{"margin_pct": 22.0}]}
        delta, segment = _margin_compression(primary, prior)
        assert delta is None

    def test_missing_data_returns_none(self):
        assert _margin_compression(None, None) == (None, None)
        assert _margin_compression({"rows": []}, None) == (None, None)
