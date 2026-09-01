"""Unit tests for web-search + file citations in app.routers.agents.

Covers:
- web_search tool results → Kimi/GPT-style clickable web source chips
  ({source_id: url, url, kind: "web"}, capped at 5, http(s) only).
- file-bearing tool results (create_artifact / docx export / read_file)
  → file source chips ({url, kind: "file"}).
- shape isolation: datasource / fetch_data_batch shapes never match the
  web branch; malformed input is skipped.
"""

from app.routers.agents import _extract_citations_from_tool_calls


class TestWebSearchCitations:
    """web_search tool results surface as clickable source chips."""

    def test_fsm_shape_web_search(self):
        tool_calls = [{
            "name": "web_search",
            "result": {
                "success": True,
                "query": "crude oil price october",
                "results": [
                    {"title": "Oil price today", "url": "https://example.com/oil", "description": "..."},
                    {"title": "Brent crude", "url": "https://example.com/brent", "description": "..."},
                ],
                "count": 2,
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert citations == [
            {
                "source_id": "https://example.com/oil",
                "source_name": "Oil price today",
                "rows": None,
                "url": "https://example.com/oil",
                "kind": "web",
            },
            {
                "source_id": "https://example.com/brent",
                "source_name": "Brent crude",
                "rows": None,
                "url": "https://example.com/brent",
                "kind": "web",
            },
        ]

    def test_legacy_shape_web_search(self):
        """Legacy tool_calls_for_frontend carries results under ``results``."""
        tool_calls = [{
            "tool_name": "web_search",
            "results": {
                "success": True,
                "query": "petrochemical market",
                "results": [
                    {"title": "C5 resin demand", "url": "https://example.com/c5", "description": "s"},
                ],
                "count": 1,
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["kind"] == "web"
        assert citations[0]["url"] == "https://example.com/c5"
        assert citations[0]["source_id"] == "https://example.com/c5"

    def test_web_results_capped_at_five(self):
        tool_calls = [{
            "name": "web_search",
            "result": {
                "query": "q",
                "results": [
                    {"title": f"r{i}", "url": f"https://example.com/{i}", "description": ""}
                    for i in range(8)
                ],
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 5
        assert citations[0]["url"] == "https://example.com/0"
        assert citations[4]["url"] == "https://example.com/4"

    def test_non_http_urls_skipped(self):
        tool_calls = [{
            "name": "web_search",
            "result": {
                "query": "q",
                "results": [
                    {"title": "bad", "url": "/api/uploads/x.txt", "description": ""},
                    {"title": "good", "url": "https://example.com/good", "description": ""},
                ],
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["url"] == "https://example.com/good"

    def test_web_dedupe_by_url(self):
        tool_calls = [
            {"name": "web_search", "result": {"query": "q", "results": [
                {"title": "a", "url": "https://example.com/x", "description": ""}]}},
            {"name": "web_search", "result": {"query": "q2", "results": [
                {"title": "a again", "url": "https://example.com/x", "description": ""}]}},
        ]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1

    def test_long_title_truncated(self):
        long_title = "x" * 300
        tool_calls = [{"name": "web_search", "result": {"query": "q", "results": [
            {"title": long_title, "url": "https://example.com/long", "description": ""}]}}]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations[0]["source_name"]) <= 121  # 120 chars + ellipsis

    def test_datasource_shape_not_treated_as_web(self):
        """fetch_data_batch nested rows carry label/rows, never url — unchanged."""
        tool_calls = [{
            "name": "fetch_data_batch",
            "result": {
                "results": [
                    {"label": "Sales", "rows": [{"a": 1}], "source_id": "kb_1"},
                ],
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0].get("kind") != "web"
        assert "url" not in citations[0]

    def test_mixed_data_and_web_turn(self):
        tool_calls = [
            {"name": "execute_query", "result": {
                "source_id": "kb_9", "source_name": "ERP", "rows": [{"a": 1}]}},
            {"name": "web_search", "result": {"query": "q", "results": [
                {"title": "t", "url": "https://example.com/t", "description": ""}]}},
        ]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert [c.get("kind") for c in citations] == [None, "web"]
        assert citations[0]["source_name"] == "ERP"
        assert citations[1]["source_name"] == "t"


class TestFileCitations:
    """File-bearing tool results surface as file source chips."""

    def test_file_url_safe_prefix(self):
        tool_calls = [{"name": "create_artifact", "result": {
            "file_url": "/api/uploads/abc123.docx", "file_name": "Report.docx"}}]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["kind"] == "file"
        assert citations[0]["url"] == "/api/uploads/abc123.docx"
        assert citations[0]["source_name"] == "Report.docx"

    def test_file_url_http_allowed(self):
        tool_calls = [{"name": "export", "result": {
            "file_url": "https://cdn.example.com/out.xlsx", "file_name": "out.xlsx"}}]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["url"] == "https://cdn.example.com/out.xlsx"

    def test_unsafe_file_url_skipped_name_kept(self):
        tool_calls = [{"name": "read_file", "result": {
            "file_url": "/etc/passwd", "file_name": "passwd"}}]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0].get("kind") == "file"
        assert "url" not in citations[0]
        assert citations[0]["source_name"] == "passwd"

    def test_file_name_only(self):
        tool_calls = [{"name": "docx_export", "result": {"file_name": "Q3 Report.docx"}}]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["source_name"] == "Q3 Report.docx"
