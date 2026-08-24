"""Tests for Format 4 (canonical Qwen3 / Hermes-Qwen <tool_call>{json}</tool_call>)
parsing in Zhanlu's tool-call extractor.

The Format 4 block is emitted by vLLM with `--tool-call-parser=qwen3_xml`
when the structured tool_calls path is bypassed (oversized tool schema,
partial completion, or as a fallback in some Qwen3 chat templates).  The
inner JSON shape matches legacy Format 3 (`Function:/Arguments:`).

These tests cover: happy path, whitespace tolerance, multiple tool calls
in one content, malformed input, mixed legacy+new formats, CJK args,
missing name, args-as-string coercion.
"""

from __future__ import annotations

import json as _json

from app.routers.agents import (
    _extract_tool_calls_from_content,
    _strip_tool_call_markup,
)


class TestFormat4Detect:
    """Tests for _extract_tool_calls_from_content with Format 4 input."""

    def test_format_4_happy_path(self):
        """Single tool call in canonical Hermes-Qwen tag format."""
        content = (
            'Let me check the weather.\n'
            '<tool_call>'
            '{"name": "get_weather", "arguments": {"city": "北京"}}'
            '</tool_call>'
        )
        calls = _extract_tool_calls_from_content(content)
        assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
        assert calls[0]["function"]["name"] == "get_weather"
        assert _json.loads(calls[0]["function"]["arguments"]) == {"city": "北京"}

    def test_format_4_with_whitespace(self):
        """Tag with whitespace drift inside and around the JSON."""
        content = (
            '<\n  tool_call\n  >\n'
            '  {"name": "f", "arguments": {"k": "v"}}\n'
            '  <\n  /tool_call\n  >'
        )
        calls = _extract_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "f"
        assert _json.loads(calls[0]["function"]["arguments"]) == {"k": "v"}

    def test_format_4_multiple_in_one_content(self):
        """Two tool calls back-to-back with prose between."""
        content = (
            '<tool_call>'
            '{"name": "first", "arguments": {}}'
            '</tool_call>'
            ' Some text between.\n'
            '<tool_call>'
            '{"name": "second", "arguments": {"x": 1}}'
            '</tool_call>'
        )
        calls = _extract_tool_calls_from_content(content)
        assert len(calls) == 2
        assert calls[0]["function"]["name"] == "first"
        assert calls[1]["function"]["name"] == "second"

    def test_format_4_malformed_json_skipped(self):
        """Truncated JSON inside the tag is skipped silently (no crash)."""
        content = (
            '<tool_call>'
            '{"name": "x", "arguments":'
            '</tool_call>'
        )
        calls = _extract_tool_calls_from_content(content)
        assert calls == []

    def test_format_4_non_dict_json_skipped(self):
        """Non-object JSON (null, array, string) is silently skipped."""
        for raw in ("null", "[1, 2, 3]", '"just a string"', "42"):
            content = f'<tool_call>{raw}</tool_call>'
            calls = _extract_tool_calls_from_content(content)
            assert calls == [], f"raw={raw!r} returned {calls!r}"

    def test_format_4_missing_name_skipped(self):
        """Tag with no `name` field is silently skipped."""
        content = '<tool_call>{"arguments": {"x": 1}}</tool_call>'
        calls = _extract_tool_calls_from_content(content)
        assert calls == []

    def test_format_4_args_as_string_normalized(self):
        """`arguments` emitted as a JSON string is coerced to {}."""
        content = '<tool_call>{"name": "f", "arguments": "{}"}</tool_call>'
        calls = _extract_tool_calls_from_content(content)
        assert len(calls) == 1
        assert _json.loads(calls[0]["function"]["arguments"]) == {}

    def test_format_4_unicode_in_args(self):
        """CJK characters in name and arguments round-trip cleanly."""
        content = (
            '<tool_call>'
            '{"name": "查询", "arguments": {"sql": "SELECT \'你好\'"}}'
            '</tool_call>'
        )
        calls = _extract_tool_calls_from_content(content)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "查询"
        assert _json.loads(calls[0]["function"]["arguments"]) == {"sql": "SELECT '你好'"}

    def test_extract_priority_order(self):
        """Content mixing Format 1 (legacy XML) AND Format 4 (Hermes tag) — Format 1 wins (extracted first)."""
        content = (
            '<function=legacy_one><parameter=arg>1</parameter></function>'
            ' some prose '
            '<tool_call>{"name": "hermes_one", "arguments": {}}</tool_call>'
        )
        calls = _extract_tool_calls_from_content(content)
        # Format 1 has its own `if tool_calls: return tool_calls` early-exit.
        # Once Format 1 finds one call, Format 4 never runs.
        names = [c["function"]["name"] for c in calls]
        assert names == ["legacy_one"], f"expected Format 1 first, got {names}"


class TestFormat4Strip:
    """Tests for _strip_tool_call_markup with Format 4 input."""

    def test_strip_removes_hermes_tag(self):
        """Tag block is fully removed; surrounding text remains concatenated."""
        content = 'preamble<tool_call>{"name":"f","arguments":{}}</tool_call>postamble'
        result = _strip_tool_call_markup(content)
        # re.sub removes the entire tag block; surrounding text concatenates.
        assert "tool_call" not in result
        assert "preamble" in result
        assert "postamble" in result

    def test_strip_handles_malformed_hermes_tag(self):
        """Unclosed tag is not stripped (best-effort strip; no crash)."""
        content = "text <tool_call> broken but no closing"
        result = _strip_tool_call_markup(content)
        # Best-effort: the regex (non-greedy) does not match; original text is kept.
        assert "text" in result
        assert "broken" in result

    def test_strip_priority_order(self):
        """Strip pipeline removes BOTH Format 1 and Format 4, plain text remains."""
        content = (
            'preamble '
            '<function=foo><parameter=x>1</parameter></function>'
            ' middle '
            '<tool_call>{"name":"bar","arguments":{}}</tool_call>'
            ' end'
        )
        result = _strip_tool_call_markup(content)
        # Both formats stripped; prose survives.
        assert "function=" not in result
        assert "tool_call" not in result
        assert "preamble" in result
        assert "middle" in result
        assert "end" in result
