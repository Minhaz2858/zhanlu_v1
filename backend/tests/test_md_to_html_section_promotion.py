"""Tests for _md_to_html orphan section-header promotion.

When the LLM forgets to add ``##`` prefixes on section names (e.g.
"Executive summary" instead of "## Executive summary"), the renderer
should auto-promote them to ``<h2>``.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.document_generator import _md_to_html


# ── Promotion cases ──────────────────────────────────────────────────────────


def test_orphan_section_promoted_to_h2():
    """Short line before blank line, followed by longer content → <h2>."""
    md = "Executive summary\n\nThe total revenue for the period was ¥265.5M across 26 products."
    html = _md_to_html(md)
    assert "<h2>Executive summary</h2>" in html
    assert "<p>Executive summary</p>" not in html


def test_key_metrics_promoted():
    md = "Key metrics\n\nTotal sales revenue is ¥265.5M across 26 products."
    html = _md_to_html(md)
    assert "<h2>Key metrics</h2>" in html


def test_changes_since_last_run_promoted():
    md = "Changes since last run\n\nVolume declined by 71.2% compared to the previous period."
    html = _md_to_html(md)
    assert "<h2>Changes since last run</h2>" in html


# ── Non-promotion cases (false-positive guards) ─────────────────────────────


def test_regular_paragraph_not_promoted():
    """A normal paragraph (ends with period, followed by more text) stays <p>."""
    md = "This is a regular paragraph.\n\nThis is another paragraph."
    html = _md_to_html(md)
    assert "<p>This is a regular paragraph.</p>" in html


def test_short_line_not_promoted_when_no_blank_before():
    """If the previous line is non-blank, don't promote."""
    md = "Some content\nShort line\n\nMore content here that is longer than the short line."
    html = _md_to_html(md)
    assert "<h2>Short line</h2>" not in html


def test_short_line_not_promoted_when_next_line_shorter():
    """If the next non-empty line is shorter, don't promote."""
    md = "\nShort line\n\nx"
    html = _md_to_html(md)
    assert "<h2>Short line</h2>" not in html


def test_line_ending_with_colon_not_promoted():
    """Lines ending with terminal punctuation (colon) are NOT promoted."""
    md = "Note:\n\nThis is a longer explanation that follows the colon line."
    html = _md_to_html(md)
    assert "<h2>Note:</h2>" not in html


def test_existing_heading_prefix_unchanged():
    """Lines that already have ## should still render as <h2>."""
    md = "## Executive summary\n\nThe revenue was ¥100M."
    html = _md_to_html(md)
    assert "<h2>Executive summary</h2>" in html


def test_long_line_not_promoted():
    """Lines over 60 chars are not promoted."""
    long_line = "A" * 61
    md = f"\n{long_line}\\n\n{'B' * 100}"
    html = _md_to_html(md)
    assert "<h2>" not in html


# ── Mixed: some promoted, some not ──────────────────────────────────────────


def test_mixed_report_format():
    """A realistic report with both promoted and non-promoted lines."""
    md = """Executive summary

The total revenue was ¥265.5M across 26 products.

Key metrics

Total sales quantity reached 40,103 units.

This line ends with a period. It should not be promoted.

Transmission Risk

The current spot price is 5,350 CNY/ton, below the average execution price."""

    html = _md_to_html(md)
    assert "<h2>Executive summary</h2>" in html
    assert "<h2>Key metrics</h2>" in html
    assert "<h2>Transmission Risk</h2>" in html
    # Regular paragraph preserved
    assert "<p>This line ends with a period. It should not be promoted.</p>" in html
