"""Tests for the ``_GENERIC_HEADING_BLOCKLIST`` + ``_title_from_prose``
fix in generation_orchestrator.

The blocklist prevents time-only fragments and pure section headers
from leaking into deliverable titles. The fix must be PRECISE:
reject generic boilerplate but allow legitimate title prefixes like
"Executive Overview" or "July Overview".
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))

from app.services.generation_orchestrator import (
    _GENERIC_HEADING_BLOCKLIST,
    _title_from_prose,
)


# ---------------------------------------------------------------------------
# _GENERIC_HEADING_BLOCKLIST
# ---------------------------------------------------------------------------

class TestBlocklistRejects:
    """Patterns the blocklist MUST reject (these are generic
    boilerplate, not real titles)."""

    def test_quarter_label_rejected(self):
        assert _GENERIC_HEADING_BLOCKLIST.match("Q3")
        assert _GENERIC_HEADING_BLOCKLIST.match("Q4 2026")

    def test_month_label_rejected(self):
        assert _GENERIC_HEADING_BLOCKLIST.match("July")
        assert _GENERIC_HEADING_BLOCKLIST.match("July 2026")
        assert _GENERIC_HEADING_BLOCKLIST.match("December")

    def test_bare_year_rejected(self):
        assert _GENERIC_HEADING_BLOCKLIST.match("2026")

    def test_executive_summary_rejected(self):
        assert _GENERIC_HEADING_BLOCKLIST.match("Executive Summary")

    def test_quarterly_report_rejected(self):
        assert _GENERIC_HEADING_BLOCKLIST.match("Quarterly Report")
        assert _GENERIC_HEADING_BLOCKLIST.match("Monthly Update")
        assert _GENERIC_HEADING_BLOCKLIST.match("Annual Review")
        assert _GENERIC_HEADING_BLOCKLIST.match("Weekly Brief")

    def test_single_word_section_header_rejected(self):
        for s in ("Report", "Summary", "Update", "Review", "Brief",
                  "Analysis", "Insights", "Snapshot", "Recap"):
            assert _GENERIC_HEADING_BLOCKLIST.match(s), f"{s!r} should be rejected"


class TestBlocklistAllows:
    """Patterns the blocklist MUST allow (legitimate title prefixes
    carrying real content)."""

    def test_executive_overview_allowed(self):
        # "Executive Overview" is a legitimate title prefix (not the
        # section-header "Executive Summary").
        assert not _GENERIC_HEADING_BLOCKLIST.match("Executive Overview")

    def test_july_overview_allowed(self):
        # Time + qualifier is fine.
        assert not _GENERIC_HEADING_BLOCKLIST.match("July Overview")

    def test_domain_metric_allowed(self):
        assert not _GENERIC_HEADING_BLOCKLIST.match("Sales Performance")
        assert not _GENERIC_HEADING_BLOCKLIST.match("Customer Churn")
        assert not _GENERIC_HEADING_BLOCKLIST.match("Inventory Turnover")

    def test_full_sentence_allowed(self):
        assert not _GENERIC_HEADING_BLOCKLIST.match(
            "Revenue grew 12% in Q3 driven by Product A."
        )


# ---------------------------------------------------------------------------
# _title_from_prose
# ---------------------------------------------------------------------------

class TestTitleFromProse:
    def test_h1_allowed_when_specific(self):
        assert _title_from_prose(
            "# Executive Overview\nSales grew 12%.",
            fallback="Fallback",
        ) == "Executive Overview"

    def test_h1_skipped_when_blocklisted(self):
        # H1 is "Q3 2026" (blocklisted) → falls through to the first
        # non-chatter sentence "Sales grew 12% in Q3."
        result = _title_from_prose(
            "# Q3 2026\nSales grew 12% in Q3.", fallback="Fallback",
        )
        assert result == "Sales grew 12% in Q3."

    def test_sentence_skipped_when_blocklisted(self):
        # First sentence "Quarterly Report" is blocklisted → falls
        # through to the next sentence.
        result = _title_from_prose(
            "Quarterly Report\nRevenue grew 12%.",
            fallback="Fallback",
        )
        assert result == "Revenue grew 12%."

    def test_fallback_when_all_blocklisted(self):
        # Both sentences are blocklisted → returns fallback.
        result = _title_from_prose(
            "Q3 2026\nExecutive Summary", fallback="Data Report",
        )
        assert result == "Data Report"

    def test_chatter_skipped(self):
        # First sentence is meta-chatter, second is the real title.
        result = _title_from_prose(
            "Let me load the data...\nRevenue grew 12% in Q3.",
            fallback="Fallback",
        )
        assert result == "Revenue grew 12% in Q3."

    def test_empty_text_returns_fallback(self):
        assert _title_from_prose("", fallback="My Report") == "My Report"

    def test_truncates_long_titles(self):
        long = "a" * 200
        result = _title_from_prose(f"# {long}", fallback="Fallback")
        assert len(result) == 120
