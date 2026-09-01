"""Regression tests for Phase 2: Response Quality unification.

Covers:
- evaluate_response_quality() standalone function
- citation_grounding.py parse + resolve
- QUALITY_EVAL_ALL_PATHS feature gate
"""

import pytest
from unittest.mock import patch


# ── evaluate_response_quality() ───────────────────────────────────────


class TestStandaloneQualityEval:
    """Verify the standalone evaluate_response_quality() works outside FSM."""

    def test_returns_accept_when_disabled(self):
        """When SYNEXIA_QUALITY_EVAL_ENABLED=False, returns accept verdict."""
        from app.services.synexia.quality_eval import evaluate_response_quality

        with patch("app.config.settings") as mock_settings:
            mock_settings.SYNEXIA_QUALITY_EVAL_ENABLED = False
            mock_settings.QUALITY_EVAL_ALL_PATHS = True
            result = evaluate_response_quality(
                user_message="hello",
                assistant_text="world",
            )
            assert result.verdict == "accept"
            assert result.final_text == "world"

    def test_returns_accept_when_all_paths_disabled(self):
        """When QUALITY_EVAL_ALL_PATHS=False, returns accept verdict."""
        from app.services.synexia.quality_eval import evaluate_response_quality

        with patch("app.config.settings") as mock_settings:
            mock_settings.SYNEXIA_QUALITY_EVAL_ENABLED = True
            mock_settings.QUALITY_EVAL_ALL_PATHS = False
            result = evaluate_response_quality(
                user_message="hello",
                assistant_text="world",
            )
            assert result.verdict == "accept"
            assert result.final_text == "world"

    def test_function_is_callable_with_minimal_args(self):
        """The standalone function accepts minimal (user_message, assistant_text)."""
        from app.services.synexia.quality_eval import evaluate_response_quality
        assert callable(evaluate_response_quality)


# ── citation_grounding.py ─────────────────────────────────────────────


class TestCitationParsing:
    """Verify citation marker parsing and resolution."""

    def test_parse_single_source_citation(self):
        from app.services.citation_grounding import parse_citations
        text = "The market grew 5% in Q3 [source: Q3 Report]. This is significant."
        citations = parse_citations(text)
        assert len(citations) == 1
        assert citations[0].source_name == "Q3 Report"
        assert citations[0].label == "source: Q3 Report"

    def test_parse_multiple_citations(self):
        from app.services.citation_grounding import parse_citations
        text = (
            "Revenue up 10% [source: Annual Report]. "
            "Costs down 3% [source: Cost Analysis]."
        )
        citations = parse_citations(text)
        assert len(citations) == 2
        assert citations[0].source_name == "Annual Report"
        assert citations[1].source_name == "Cost Analysis"

    def test_parse_citation_alternate_syntax(self):
        from app.services.citation_grounding import parse_citations
        text = "See details in [citation: Research Paper 2025]."
        citations = parse_citations(text)
        assert len(citations) == 1
        assert citations[0].source_name == "Research Paper 2025"

    def test_parse_no_citations_returns_empty(self):
        from app.services.citation_grounding import parse_citations
        text = "No citations here, just plain text."
        citations = parse_citations(text)
        assert citations == []

    def test_parse_citations_preserves_positions(self):
        from app.services.citation_grounding import parse_citations
        text = "Hello [source: doc1] world"
        citations = parse_citations(text)
        assert citations[0].position_start > 0
        assert citations[0].position_end > citations[0].position_start

    def test_resolve_citations_with_registry(self):
        from app.services.citation_grounding import resolve_citations, parse_citations
        text = "See [source: Q3 Report] and [source: Analysis]."
        citations = parse_citations(text)

        registry = {
            "Q3 Report": {"url": "https://x.com/q3.pdf", "page": 5, "type": "document"},
            "Analysis": {"url": "https://x.com/analysis.pdf", "type": "document"},
        }
        resolved = resolve_citations(citations, registry)
        assert resolved[0].url == "https://x.com/q3.pdf"
        assert resolved[0].page == 5
        assert resolved[1].url == "https://x.com/analysis.pdf"

    def test_annotate_tool_result_appends_marker(self):
        from app.services.citation_grounding import annotate_tool_result
        annotated = annotate_tool_result(
            "Revenue grew 5% in Q3.",
            source_name="Q3 Report.pdf",
            url="https://x.com/report.pdf",
            page=5,
        )
        assert "[source: Q3 Report.pdf]" in annotated
        assert "url: https://x.com/report.pdf" in annotated
        assert "page: 5" in annotated

    def test_citation_instruction_returns_string(self):
        from app.services.citation_grounding import citation_instruction
        instr = citation_instruction()
        assert "[source:" in instr
        assert "name]" in instr
