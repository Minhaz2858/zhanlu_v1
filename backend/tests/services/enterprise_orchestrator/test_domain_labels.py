"""Tests for the deterministic domain→section-label mapping.

Design spec reference: §7 Domain → Section Label Mapping.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

from app.services.enterprise_orchestrator.domain_labels import (
    DOMAIN_SECTION_LABELS,
    get_section_labels,
    known_domains,
)


class TestEightDomainsCovered:
    def test_all_eight_present(self):
        for d in {
            "supply_chain", "financial_performance", "logistics",
            "risk_management", "sales_operations", "hr",
            "procurement", "generic",
        }:
            assert d in DOMAIN_SECTION_LABELS
            assert "risk_section" in DOMAIN_SECTION_LABELS[d]
            assert "drivers_section" in DOMAIN_SECTION_LABELS[d]

    def test_known_domains_returns_eight(self):
        assert len(known_domains()) == 8


class TestKnownDomainLabels:
    def test_supply_chain(self):
        labels = get_section_labels("supply_chain")
        assert labels["risk_section"] == "Transmission Risk"
        assert labels["drivers_section"] == "Supply-Demand Drivers"

    def test_financial_performance(self):
        labels = get_section_labels("financial_performance")
        assert labels["risk_section"] == "Cash-Flow Exposure"
        assert labels["drivers_section"] == "Margin & Cost Drivers"

    def test_sales_operations(self):
        labels = get_section_labels("sales_operations")
        assert "Pipeline" in labels["risk_section"]
        assert "Funnel" in labels["drivers_section"]


class TestGenericFallback:
    def test_unknown_domain_falls_back(self):
        labels = get_section_labels("hack_me")
        assert labels == DOMAIN_SECTION_LABELS["generic"]

    def test_none_falls_back(self):
        assert get_section_labels(None) == DOMAIN_SECTION_LABELS["generic"]

    def test_empty_string_falls_back(self):
        assert get_section_labels("") == DOMAIN_SECTION_LABELS["generic"]
