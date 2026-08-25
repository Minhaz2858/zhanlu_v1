"""Tests for _generate_meaningful_title and _extract_period — the
deliverable-title builders introduced 2026-08-24.

The user reported that DOCX/PDF/PPTX deliverables echoed the raw query as the
title ("i want July 2026 sales report (volume, revenue, margin, inventory) in
docx file"). These tests pin the new behavior: conversational wrappers,
file-format suffixes and parenthetical metric lists are stripped, date/period
entities are extracted and normalized, and empty input falls back to prose or
the fallback parameter.
"""

import os
import sys

# ── Path setup for in-container pytest ──────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/venv/lib/python3.11/site-packages")

import pytest

from app.services.generation_orchestrator import (
    _generate_meaningful_title,
    _extract_period,
)


# ═══════════════════════════════════════════════════════════════════
# 1. Conversational prefix stripping
# ═══════════════════════════════════════════════════════════════════

class TestStripsConversationalPrefix:
    def test_strips_i_want_prefix(self):
        assert (
            _generate_meaningful_title("i want july 2026 sales report in docx file")
            == "July 2026 sales report"
        )

    def test_strips_i_need_prefix(self):
        assert _generate_meaningful_title("i need the revenue report") == "The revenue report"

    def test_strips_please_give_me(self):
        assert _generate_meaningful_title("please give me the sales report") == "The sales report"

    def test_strips_can_you(self):
        assert _generate_meaningful_title("can you give me the inventory report") == "The inventory report"

    def test_strips_show_make_generate_create(self):
        assert _generate_meaningful_title("show me the margin report") == "The margin report"
        assert _generate_meaningful_title("make me a volume report") == "A volume report"
        assert _generate_meaningful_title("generate the sales report") == "The sales report"
        assert _generate_meaningful_title("create the revenue report") == "The revenue report"

    def test_prefix_stripping_is_case_insensitive(self):
        assert (
            _generate_meaningful_title("I WANT JULY 2026 SALES REPORT IN DOCX FILE")
            == "July 2026 sales report"
        )


# ═══════════════════════════════════════════════════════════════════
# 2. File-format suffix stripping
# ═══════════════════════════════════════════════════════════════════

class TestStripsFileFormatSuffix:
    def test_strips_docx_file_suffix(self):
        assert _generate_meaningful_title("july 2026 sales report in docx file") == "July 2026 sales report"

    def test_strips_word_document_suffix(self):
        assert _generate_meaningful_title("sales report as a word document") == "Sales report"

    def test_strips_pdf_pptx_xlsx_suffix(self):
        assert _generate_meaningful_title("i want the sales report in pdf") == "The sales report"
        assert _generate_meaningful_title("i want the sales report as pptx") == "The sales report"
        assert _generate_meaningful_title("i want the inventory in excel") == "The inventory"

    def test_strips_markdown_suffix(self):
        assert _generate_meaningful_title("write the sales report in markdown") == "The sales report"


# ═══════════════════════════════════════════════════════════════════
# 3. Parenthetical metric list stripping
# ═══════════════════════════════════════════════════════════════════

class TestStripsParentheticalMetrics:
    def test_strips_metric_parens_full_example(self):
        """The exact user-reported failing query."""
        assert (
            _generate_meaningful_title(
                "i want July 2026 sales report (volume, revenue, margin, inventory) in docx file"
            )
            == "July 2026 sales report"
        )

    def test_strips_mid_sentence_parens(self):
        assert (
            _generate_meaningful_title("sales report (all metrics) for july 2026")
            == "July 2026 sales report"
        )

    def test_multiple_paren_groups_stripped(self):
        assert (
            _generate_meaningful_title("i want (a) sales (volume) report in docx")
            == "Sales report"
        )


# ═══════════════════════════════════════════════════════════════════
# 4. Period detection & prefixing
# ═══════════════════════════════════════════════════════════════════

class TestExtractPeriod:
    def test_month_year_period(self):
        assert _extract_period("july 2026 sales report") == "July 2026"

    def test_quarter_year_period(self):
        assert _extract_period("q3 2026 revenue report") == "Q3 2026"

    def test_last_n_days_period(self):
        assert _extract_period("sales for last 30 days") == "Last 30 days"

    def test_this_month_period(self):
        assert _extract_period("inventory this month") == "This month"

    def test_no_period_returns_empty(self):
        assert _extract_period("sales report") == ""

    def test_period_case_insensitive(self):
        assert _extract_period("JULY 2026 report") == "July 2026"

    def test_period_prefixed_into_title(self):
        assert (
            _generate_meaningful_title("i want the sales report for q3 2026 in docx")
            == "Q3 2026 the sales report"
        )


# ═══════════════════════════════════════════════════════════════════
# 5. Capitalization, whitespace normalization, length clamping
# ═══════════════════════════════════════════════════════════════════

class TestCapitalizeNormalizeClamp:
    def test_capitalizes_first_letter(self):
        assert _generate_meaningful_title("sales report") == "Sales report"

    def test_normalizes_internal_whitespace(self):
        assert _generate_meaningful_title("i want   the   sales   report") == "The sales report"

    def test_clamps_to_60_chars(self):
        long = "i want " + "very detailed quarterly analysis of the " * 5 + "sales revenue in docx file"
        result = _generate_meaningful_title(long)
        assert len(result) <= 60
        assert result.endswith("…")

    def test_short_title_untouched(self):
        assert _generate_meaningful_title("sales report") == "Sales report"


# ═══════════════════════════════════════════════════════════════════
# 6. Fallback chain: user message → assistant prose → fallback param
# ═══════════════════════════════════════════════════════════════════

class TestFallbackChain:
    def test_empty_message_uses_prose_h1(self):
        assert (
            _generate_meaningful_title("", assistant_content="# Executive Overview\nSales grew 12%.")
            == "Executive Overview"
        )

    def test_empty_message_no_prose_uses_fallback(self):
        assert _generate_meaningful_title("", assistant_content="", fallback="Data report") == "Data report"

    def test_wrapper_only_message_falls_back_to_prose(self):
        assert (
            _generate_meaningful_title("i want in docx file", assistant_content="# July Overview")
            == "July Overview"
        )

    def test_wrapper_only_without_prose_uses_fallback(self):
        assert (
            _generate_meaningful_title("i want in docx file", assistant_content="", fallback="Data report")
            == "Data report"
        )

    def test_whitespace_only_uses_fallback(self):
        assert _generate_meaningful_title("   ", assistant_content="", fallback="Data report") == "Data report"

    def test_default_fallback_used_when_omitted(self):
        assert _generate_meaningful_title("", assistant_content="") == "Data report"
