"""Regression tests for citation_grounding.py (Part 2 — Phase 2 response quality)."""

from app.services.citation_grounding import (
    Citation,
    annotate_tool_result,
    parse_citations,
    resolve_citations,
    citation_instruction,
)


class TestAnnotateToolResult:
    """Tests for annotate_tool_result."""

    def test_adds_source_marker(self):
        result = annotate_tool_result("Some content.", source_name="Q3 Report.pdf")
        assert "[source: Q3 Report.pdf]" in result
        assert "Some content." in result

    def test_adds_url_footer(self):
        result = annotate_tool_result(
            "Content.", source_name="Report", url="https://example.com/report"
        )
        assert "[source: Report]" in result
        assert "url: https://example.com/report" in result

    def test_adds_page_footer(self):
        result = annotate_tool_result(
            "Content.", source_name="Report", page=5
        )
        assert "page: 5" in result

    def test_adds_metadata_footer(self):
        result = annotate_tool_result(
            "Content.", source_name="Report",
            metadata={"author": "John", "date": "2026-01-01"},
        )
        assert "author: John" in result
        assert "date: 2026-01-01" in result

    def test_skips_url_and_source_name_in_metadata(self):
        """url and source_name in metadata dict should be skipped in footer."""
        result = annotate_tool_result(
            "Content.", source_name="Report", url="https://x.com",
            metadata={"url": "ignored", "source_name": "also_ignored", "extra": "keep"},
        )
        assert "extra: keep" in result
        assert "ignored" not in result

    def test_no_footer_when_no_metadata(self):
        result = annotate_tool_result("Content.", source_name="Report")
        assert "<!-- citation_meta:" not in result

    def test_limits_footer_parts_to_10(self):
        extra = {f"key{i}": f"val{i}" for i in range(20)}
        result = annotate_tool_result("C", source_name="R", metadata=extra)
        # Footer line must exist and not crash regardless of count
        assert "[source: R]" in result


class TestParseCitations:
    """Tests for parse_citations."""

    def test_parses_single_source_marker(self):
        text = "The report says X [source: Q3 Report.pdf]."
        citations = parse_citations(text)
        assert len(citations) == 1
        assert citations[0].source_name == "Q3 Report.pdf"
        assert citations[0].label == "source: Q3 Report.pdf"

    def test_parses_citation_marker(self):
        text = "See [citation: Annual Review] for more."
        citations = parse_citations(text)
        assert len(citations) == 1
        assert citations[0].label == "citation: Annual Review"
        assert citations[0].source_type == "document"

    def test_parses_multiple_citations(self):
        text = "[source: Doc A] and [source: Doc B] and [citation: Doc C]"
        citations = parse_citations(text)
        assert len(citations) == 3
        names = [c.source_name for c in citations]
        assert "Doc A" in names
        assert "Doc B" in names
        assert "Doc C" in names

    def test_returns_empty_for_no_citations(self):
        citations = parse_citations("No citations here.")
        assert citations == []

    def test_returns_empty_for_empty_text(self):
        assert parse_citations("") == []

    def test_captures_positions(self):
        text = "abc [source: X] def"
        citations = parse_citations(text)
        assert citations[0].position_start == 4  # '[' at index 4
        assert citations[0].position_end == len("abc [source: X]")  # after ']'

    def test_case_insensitive_marker(self):
        text = "See [SOURCE: Report] here."
        citations = parse_citations(text)
        assert len(citations) == 1
        assert citations[0].source_name == "Report"

    def test_trims_whitespace_in_source_name(self):
        text = "[source:   padded name  ]"
        citations = parse_citations(text)
        assert citations[0].source_name == "padded name"


class TestResolveCitations:
    """Tests for resolve_citations."""

    def test_resolves_against_registry(self):
        citations = [Citation(label="s: A", source_name="Doc A")]
        registry = {"Doc A": {"url": "https://a.com", "page": 3, "type": "document"}}
        resolved = resolve_citations(citations, registry)
        assert resolved[0].url == "https://a.com"
        assert resolved[0].page == 3
        assert resolved[0].source_type == "document"

    def test_unmatched_citation_stays_unresolved(self):
        citations = [Citation(label="s: X", source_name="Unknown")]
        resolved = resolve_citations(citations, {"Doc A": {"url": "https://a.com"}})
        assert resolved[0].url == ""

    def test_case_insensitive_match(self):
        citations = [Citation(label="s: a", source_name="doc a")]
        registry = {"Doc A": {"url": "https://a.com"}}
        resolved = resolve_citations(citations, registry)
        assert resolved[0].url == "https://a.com"

    def test_returns_same_list(self):
        """Should return the same list (mutated in place)."""
        citations = [Citation(label="s: A", source_name="Doc A")]
        result = resolve_citations(citations, {})
        assert result is citations


class TestCitationInstance:
    """Tests for Citation dataclass."""

    def test_to_dict(self):
        c = Citation(
            label="source: Report",
            source_name="Report",
            source_type="url",
            url="https://x.com",
            page=5,
            chunk_index=1,
            position_start=0,
            position_end=15,
        )
        d = c.to_dict()
        assert d["label"] == "source: Report"
        assert d["source_name"] == "Report"
        assert d["source_type"] == "url"
        assert d["url"] == "https://x.com"
        assert d["page"] == 5


class TestCitationInstruction:
    """Tests for citation_instruction."""

    def test_returns_nonempty_string(self):
        instruction = citation_instruction()
        assert isinstance(instruction, str)
        assert len(instruction) > 0
        assert "source:" in instruction.lower()

    def test_contains_fabrication_warning(self):
        instruction = citation_instruction()
        assert "not fabricate" in instruction.lower() or "do not" in instruction.lower()
