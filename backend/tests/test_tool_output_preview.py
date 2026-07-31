"""Tests for tool command/output summarizers powering expandable steps.

Claude-style UX: clicking a step reveals WHAT the tool ran with (the
bash/code block) and a short preview of WHAT came back. These helpers
build those two strings defensively — never dumping huge result dicts.
"""

from __future__ import annotations

import pytest


class TestSummarizeToolCommand:
    def test_code_arg_wins_for_execute_code(self):
        from app.routers.agents import _summarize_tool_command
        cmd = _summarize_tool_command("execute_code", {"code": "print(1)"})
        assert cmd == "print(1)"

    def test_path_for_read_file(self):
        from app.routers.agents import _summarize_tool_command
        cmd = _summarize_tool_command("read_file", {"path": "/tmp/data.csv", "offset": 0})
        assert cmd == "/tmp/data.csv"

    def test_query_for_web_search(self):
        from app.routers.agents import _summarize_tool_command
        cmd = _summarize_tool_command("web_search", {"query": "erp demo data"})
        assert cmd == "erp demo data"

    def test_fallback_to_compact_json(self):
        from app.routers.agents import _summarize_tool_command
        cmd = _summarize_tool_command("some_tool", {"foo": "bar"})
        assert cmd is not None and "foo" in cmd

    def test_none_or_empty_args_returns_none(self):
        from app.routers.agents import _summarize_tool_command
        assert _summarize_tool_command("execute_code", None) is None
        assert _summarize_tool_command("execute_code", {}) is None

    def test_long_command_is_capped(self):
        from app.routers.agents import _summarize_tool_command, _MAX_COMMAND_CHARS
        cmd = _summarize_tool_command("execute_code", {"code": "x" * 5000})
        assert cmd is not None
        assert len(cmd) <= _MAX_COMMAND_CHARS + 1  # +1 for the ellipsis

    def test_non_dict_args_returns_none(self):
        from app.routers.agents import _summarize_tool_command
        assert _summarize_tool_command("execute_code", "not-a-dict") is None


class TestSummarizeToolOutput:
    def test_output_key_wins(self):
        from app.routers.agents import _summarize_tool_output
        out = _summarize_tool_output({"success": True, "output": "rows: 42"})
        assert out == "rows: 42"

    def test_failure_surfaces_error(self):
        from app.routers.agents import _summarize_tool_output
        out = _summarize_tool_output({"success": False, "error": "boom"})
        assert out == "boom"

    def test_failure_without_error_gives_generic(self):
        from app.routers.agents import _summarize_tool_output
        out = _summarize_tool_output({"success": False})
        assert out == "Tool call failed"

    def test_non_dict_returns_none(self):
        from app.routers.agents import _summarize_tool_output
        assert _summarize_tool_output(None) is None
        assert _summarize_tool_output("nope") is None

    def test_never_dumps_huge_result_dict(self):
        """Artifact results can be megabytes — the preview must stay small."""
        from app.routers.agents import _summarize_tool_output, _MAX_OUTPUT_CHARS
        big = {"success": True, "output": "y" * 100_000, "artifact_id": "a1"}
        out = _summarize_tool_output(big)
        assert out is not None
        assert len(out) <= _MAX_OUTPUT_CHARS + 1

    def test_result_without_known_keys_returns_none(self):
        from app.routers.agents import _summarize_tool_output
        assert _summarize_tool_output({"success": True, "artifact_id": "a1"}) is None

    def test_whitespace_only_values_skipped(self):
        from app.routers.agents import _summarize_tool_output
        assert _summarize_tool_output({"success": True, "output": "   "}) is None
