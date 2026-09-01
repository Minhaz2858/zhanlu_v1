"""Tests for extract_requested_metrics / _METRIC_KEYWORDS in agent_prompts.py.

Introduced 2026-08-24: when a user asks for a report with N metrics (volume,
revenue, margin, inventory), the agent must run at least one data query per
metric. This helper detects which business metrics a user message requests so
the prompt can enforce per-metric collection.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.agent_prompts import (
    _METRIC_KEYWORDS,
    extract_requested_metrics,
)


class TestMetricCategories:
    """Each of the four core metric categories is detected by its keywords."""

    def test_volume_detected(self):
        assert "volume" in extract_requested_metrics("sales volume report")

    def test_volume_synonyms(self):
        for kw in ("qty", "quantity", "units", "shipped", "tons"):
            assert "volume" in extract_requested_metrics(f"report on {kw}")

    def test_revenue_detected(self):
        assert "revenue" in extract_requested_metrics("revenue report")

    def test_revenue_synonyms(self):
        for kw in ("sales amount", "income", "turnover"):
            assert "revenue" in extract_requested_metrics(f"report on {kw}")

    def test_margin_detected(self):
        assert "margin" in extract_requested_metrics("margin report")

    def test_margin_synonyms(self):
        for kw in ("profit", "gross profit", "markup"):
            assert "margin" in extract_requested_metrics(f"report on {kw}")

    def test_inventory_detected(self):
        assert "inventory" in extract_requested_metrics("inventory report")

    def test_inventory_synonyms(self):
        for kw in ("stock", "on hand", "in stock"):
            assert "inventory" in extract_requested_metrics(f"report on {kw}")

    def test_all_metrics_detected_in_one_message(self):
        result = extract_requested_metrics(
            "i want July 2026 sales report (volume, revenue, margin, inventory) in docx file"
        )
        assert result == ["volume", "revenue", "margin", "inventory"]

    def test_case_insensitive(self):
        assert "volume" in extract_requested_metrics("REVENUE AND VOLUME")


class TestOrderingAndEdgeCases:
    """Preserves user-intent order; handles degenerate input gracefully."""

    def test_preserves_user_order(self):
        result = extract_requested_metrics("margin, then volume, then revenue")
        assert result.index("margin") < result.index("volume") < result.index("revenue")

    def test_no_metrics_returns_empty(self):
        assert extract_requested_metrics("give me a summary") == []

    def test_empty_message_returns_empty(self):
        assert extract_requested_metrics("") == []

    def test_whitespace_message_returns_empty(self):
        assert extract_requested_metrics("   ") == []

    def test_non_english_message_returns_empty(self):
        # Chinese keywords are handled by the DB, not the English prompt helper.
        assert extract_requested_metrics("给我七月份的销售报告") == []

    def test_no_false_positive_on_substring(self):
        # "marginal" must not trigger "margin"; "inventive" must not trigger inventory.
        assert "margin" not in extract_requested_metrics("marginal growth analysis")
        assert "inventory" not in extract_requested_metrics("inventive product design")

    def test_keywords_are_plural_aware(self):
        # "profits" plural should still map to margin via "profit".
        assert "margin" in extract_requested_metrics("profits report")


class TestMetricKeywordsTable:
    """The _METRIC_KEYWORDS table is sane and stable."""

    def test_four_core_categories(self):
        assert set(_METRIC_KEYWORDS.keys()) == {"volume", "revenue", "margin", "inventory"}

    def test_all_keyword_lists_non_empty(self):
        for kws in _METRIC_KEYWORDS.values():
            assert kws
