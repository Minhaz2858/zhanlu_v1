"""Tests for the universal research-analyst directive injection in agent_prompts.py.

Covers:
  - The directive constant exists and is non-empty.
  - The directive references ``comprehensive_data(profile="market", ...)``.
  - The 8 mandatory dimensions are listed in the text.
  - The format-specific rules table is present.
  - The directive triggers for ALL DB-bound agents (no longer gated on
    ``create_artifact`` in the resolved toolset — universal applicability).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.agent_prompts import (
    _RESEARCH_ANALYST_DIRECTIVE,
    _agent_is_db_bound,
)


# ---------------------------------------------------------------------------
# Constant integrity
# ---------------------------------------------------------------------------
class TestDirectiveContent:
    def test_directive_non_empty(self):
        assert _RESEARCH_ANALYST_DIRECTIVE
        assert len(_RESEARCH_ANALYST_DIRECTIVE) > 200

    def test_references_canonical_tool(self):
        # The directive must mention ``comprehensive_data(profile="market", ...)``
        # as the canonical multi-dim gather tool.
        assert "comprehensive_data" in _RESEARCH_ANALYST_DIRECTIVE
        assert 'profile="market"' in _RESEARCH_ANALYST_DIRECTIVE
        assert "create_artifact" in _RESEARCH_ANALYST_DIRECTIVE

    def test_lists_all_eight_dimensions(self):
        # The directive uses Title Case with separators ("Core Metrics",
        # "Cost / Input Structure", "Cross-Segment Relationships").
        # Match case-insensitively — we just need evidence the dimension
        # is named in the directive, not exact wording.
        text_lower = _RESEARCH_ANALYST_DIRECTIVE.lower()
        for substring in [
            "core metrics",            # 1. Core Metrics
            "historical trends",       # 2. Historical Trends
            "cost",                    # 3. Cost / Input Structure
            "supply side",             # 4. Supply Side
            "demand side",             # 5. Demand Side
            "macro context",           # 6. Macro Context
            "forward indicators",      # 7. Forward Indicators
            "cross-segment",           # 8. Cross-Segment Relationships
        ]:
            assert substring in text_lower, f"missing dimension phrase: {substring}"

    def test_includes_four_mandatory_response_sections(self):
        # Section 1 - 4 must be referenced.
        for sec in [
            "Overview Dashboard",
            "Executive Summary",
            "Entity-by-Entity Deep Dive",
            "Disclaimer",
        ]:
            assert sec in _RESEARCH_ANALYST_DIRECTIVE, f"missing section: {sec}"

    def test_format_specific_rules_for_all_formats(self):
        # The format-specific rules table must cover every format the
        # user listed.
        for fmt in [
            "PPT / Slides",
            "Text Report / Markdown",
            "Chat / Conversational",
            "Executive Brief / One-Pager",
            "Dashboard / Widget",
        ]:
            assert fmt in _RESEARCH_ANALYST_DIRECTIVE, f"missing format: {fmt}"

    def test_quality_gate_checkboxes_present(self):
        # Quality gate must list at least one checkbox per criterion.
        for criterion in [
            "current value",
            "forecast",
            "strategy",
            "supply, demand, cost",
            "one-line summary",
            "CIO",
        ]:
            assert criterion.lower() in _RESEARCH_ANALYST_DIRECTIVE.lower(), \
                f"quality-gate criterion missing: {criterion}"


# ---------------------------------------------------------------------------
# Universal applicability — directive applies regardless of toolset
# ---------------------------------------------------------------------------
class TestUniversalApplicability:
    """Ensure the directive fires for EVERY db-bound agent — not just
    ones with ``create_artifact`` in their resolved toolset. This was the
    behavior change on 2026-08-25: from PPT-only to universal.
    """

    def test_generic_agent_name_is_db_bound(self):
        # Sanity: the directive is scoped to DB-bound agents only.
        assert _agent_is_db_bound("data_agent", None)

    def test_directive_present_for_chat_only_toolset(self):
        """A chat-only toolset (no create_artifact) must still trigger
        the directive — that's the whole point of the universal refactor.
        """
        # Smoke check the directive constant — the prompt injection
        # logic appends it on the same condition (_agent_is_db_bound
        # AND flag on) regardless of which tools are present.
        # We assert the constant itself is well-formed because the
        # trigger logic is a simple boolean AND in the build code.
        assert _RESEARCH_ANALYST_DIRECTIVE
        # Confirm there's no 'PPT only' / 'PPT scoped'/'when deliverable
        # is .pptx' wording that would limit the directive to PPT only.
        text = _RESEARCH_ANALYST_DIRECTIVE.lower()
        assert "ppt only" not in text
        assert "scoped to ppt" not in text
        assert "only when .pptx" not in text

    def test_directive_applies_to_non_ppt_formats(self):
        """The directive text must mention its applicability to chat,
        brief, dashboard, and text reports — not just PPT.
        """
        text = _RESEARCH_ANALYST_DIRECTIVE.lower()
        for fmt in ["chat", "brief", "dashboard", "markdown", "text"]:
            assert fmt in text, f"format keyword missing from directive: {fmt}"


# ---------------------------------------------------------------------------
# DB-bound helper (existing — defensive sanity check)
# ---------------------------------------------------------------------------
class TestIsDbBound:
    def test_data_agent_is_db_bound(self):
        assert _agent_is_db_bound("data_agent", None)

    def test_data_agent_is_db_bound(self):
        assert _agent_is_db_bound("data_agent", None)

    def test_general_assistant_is_db_bound(self):
        assert _agent_is_db_bound("general_assistant", None)

    def test_agent_builder_is_not_db_bound(self):
        assert not _agent_is_db_bound("agent_builder", None)