"""Tests for preamble-pattern detection in synthesis LLM output.

Root cause (user screenshot 2026-08-25):
- qwen3.6-27b query: "make a sales deck for Q3 with a beautiful design"
- LLM produced preamble response: "Looking at your sales data, I'll create
  a beautiful Q3 2026 sales deck for you. I have the data and design
  system ready. Generating the Q3 2026 sales deck now with a clean,
  corporate blue theme."
- This response (240 chars) passes the `>50 chars` check and the user
  sees an EMPTY PROMISE instead of a real analysis.

Why the existing patterns missed it:
- _APOLOGY_PATTERNS checks for "I couldn't", "I apologize", etc.
- _BOUNCE_BACK_PATTERN_RE checks for "I will create a (summary|chart|report|breakdown)"
- User said "I'll create a ... sales DECK" — not in the list!
- Also missed: "Generating the ... now"

Fix: Add a PREAMBLE pattern that catches:
- "I will create a deck/presentation/document/visualization"
- "Generating the X now"
- "Let me create..."
- "I am going to..."

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-v', 'tests/test_preamble_pattern_detection.py']))'
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Test 1: the user's exact preamble response must be detected ────────────


def test_user_preamble_response_is_detected():
    """The exact preamble the user saw in the screenshot must trigger
    the deterministic fallback (i.e. be detected as a bad response)."""
    from app.services.agent_loop.fallbacks import _BOUNCE_BACK_PATTERN_RE
    user_response = (
        "Looking at your sales data, I'll create a beautiful Q3 2026 "
        "sales deck for you. I have the data and design system ready. "
        "Generating the Q3 2026 sales deck now with a clean, corporate "
        "blue theme."
    )
    is_preamble = bool(_BOUNCE_BACK_PATTERN_RE.search(user_response))
    assert is_preamble, (
        f"User's preamble response should be detected as bad. Got no match. "
        f"Pattern needs to catch: 'I'll create a sales deck' and "
        f"'Generating the Q3 2026 sales deck now'."
    )


# ── Test 2: legitimate analysis must NOT be detected as preamble ───────────


def test_legitimate_analysis_not_detected():
    """A real analysis with markdown headers and data must NOT be
    flagged as a preamble."""
    from app.services.agent_loop.fallbacks import _BOUNCE_BACK_PATTERN_RE
    real_analysis = (
        "## Executive Summary\n"
        "Contract performance for July 2026: 218 contracts, "
        "¥362.4M total revenue, 51,602 units shipped.\n\n"
        "## Key Metrics\n"
        "- Total revenue: ¥362.4M\n"
        "- Number of records: 218\n\n"
        "## Detailed Breakdown\n"
        "| contract_number | total_amount |\n"
        "| --- | --- |\n"
        "| YSK-001 | 595,200 |\n"
    )
    is_preamble = bool(_BOUNCE_BACK_PATTERN_RE.search(real_analysis))
    assert not is_preamble, (
        "Real analysis with markdown headers and data should NOT be "
        "flagged as a preamble. The pattern is too aggressive."
    )


# ── Test 3: other "I'll create X" patterns must be detected ───────────────


def test_create_presentation_detected():
    """'I will create a presentation' must be detected."""
    from app.services.agent_loop.fallbacks import _BOUNCE_BACK_PATTERN_RE
    response = "I have the data ready. I will create a presentation for you."
    is_preamble = bool(_BOUNCE_BACK_PATTERN_RE.search(response))
    assert is_preamble, (
        f"'I will create a presentation' should be detected. Got no match."
    )


def test_generate_document_detected():
    """'Generating the document now' must be detected."""
    from app.services.agent_loop.fallbacks import _BOUNCE_BACK_PATTERN_RE
    response = "I have the data. Generating the document now."
    is_preamble = bool(_BOUNCE_BACK_PATTERN_RE.search(response))
    assert is_preamble, (
        f"'Generating the document now' should be detected. Got no match."
    )


# ── Test 4: simple statement must NOT be detected ─────────────────────────


def test_simple_statement_not_detected():
    """A short factual statement should not be detected as preamble."""
    from app.services.agent_loop.fallbacks import _BOUNCE_BACK_PATTERN_RE
    response = "I retrieved 234 rows of contract performance data."
    is_preamble = bool(_BOUNCE_BACK_PATTERN_RE.search(response))
    assert is_preamble, (
        f"'I retrieved 234 rows' should match the existing bounce-back "
        f"pattern. If this fails, the existing pattern is broken."
    )
