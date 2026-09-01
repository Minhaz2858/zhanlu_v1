"""Tests for title_builder (Phase 1B).

Design spec §12: enterprise titles must be "{period} {domain_label}
Report" — never "Executive Summary" / "Key Metrics" / etc.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.enterprise_orchestrator.title_builder import (
    build_enterprise_title,
    _GENERIC_HEADING_BLOCKLIST,
    sanitize_title,
)


class TestBlocklist:
    def test_known_generic_headings_blocked(self):
        for bad in [
            "Executive Summary", "Key Metrics",
            "Breakdown by Dimension", "Anomalies & Risks",
            "Recommended Actions", "Appendix Note",
            "Operational Drivers", "Primary Metric Breakdown",
            "Segment Decomposition",
        ]:
            assert bad.lower() in _GENERIC_HEADING_BLOCKLIST

    def test_sanitize_drops_blocklisted(self):
        assert sanitize_title("Executive Summary") == ""
        assert sanitize_title("KEY METRICS") == ""
        assert sanitize_title("Recommended Actions") == ""

    def test_sanitize_keeps_real_titles(self):
        assert sanitize_title("July 2026 Sales Report") == "July 2026 Sales Report"
        assert sanitize_title("Last 30 days Financial Performance Report") == (
            "Last 30 days Financial Performance Report"
        )


class TestBuildEnterpriseTitle:
    def test_period_and_domain(self):
        title = build_enterprise_title(
            period_label="Last 30 days",
            domain_label="Financial Performance",
        )
        assert title == "Last 30 days Financial Performance Report"

    def test_specific_date_range(self):
        title = build_enterprise_title(
            period_label="2026-07-21 to 2026-08-20",
            domain_label="Supply Chain",
        )
        assert title == "2026-07-21 to 2026-08-20 Supply Chain Report"

    def test_generic_domain_yields_fallback_label(self):
        title = build_enterprise_title(
            period_label="Q3 2026",
            domain_label=None,
        )
        assert "Report" in title
        assert "Q3 2026" in title

    def test_blocklist_blocks_garbage_input(self):
        title = build_enterprise_title(
            period_label="Executive Summary",
            domain_label="Supply Chain",
        )
        assert "Executive Summary" not in title
        assert "Supply Chain" in title

    def test_title_is_short_enough_for_filenames(self):
        title = build_enterprise_title(
            period_label="Last 30 days",
            domain_label="A very long domain description",
        )
        assert len(title) <= 120
