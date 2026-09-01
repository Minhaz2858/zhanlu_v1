"""Tests for the ``agent_browser`` tool — unit tests for each action,
helpers, registration contract, and ``execute_tool`` dispatch.

Covers: helpers, navigate, snapshot, act, screenshot, extract, eval,
close, unknown action, missing-binary fallback, registration, and
end-to-end execute_tool dispatch.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

_SNAPSHOT_JSON = json.dumps({
    "url": "https://example.com", "title": "Example Domain",
    "elements": [{"role": "heading", "name": "Example Domain", "ref": "@e1"}],
})
_EXTRACT_TEXT = "Example Domain\n\nThis domain is for use in illustrative examples."


def _ctx(cid="conv-test-1"):
    return {"conversation_id": cid}


# ── Helpers ────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_looks_like_url(self):
        from app.services.tool_handlers.agent_browser_tool import _looks_like_url
        assert _looks_like_url("https://example.com") is True
        assert _looks_like_url("about:blank") is False
        assert _looks_like_url("@e12") is False
        assert _looks_like_url("") is False

    def test_maybe_json(self):
        from app.services.tool_handlers.agent_browser_tool import _maybe_json
        assert _maybe_json('{"a":1}') == {"a": 1}
        assert _maybe_json('[1,2]') == [1, 2]
        assert _maybe_json("plain text") == "plain text"
        assert _maybe_json("") == ""

    def test_session_isolation(self):
        from app.services.tool_handlers.agent_browser_tool import (
            _get_session, _cleanup_session,
        )
        s1 = _get_session("conv-a")
        s2 = _get_session("conv-b")
        assert s1["session_id"] != s2["session_id"]
        assert _get_session("conv-a")["session_id"] == s1["session_id"]
        _cleanup_session("conv-a")
        assert _get_session("conv-a")["session_id"] != s1["session_id"]


# ── Navigate ───────────────────────────────────────────────────────────────

class TestNavigate:
    async def test_success(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": "Opened"}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries",
                   return_value=False), \
             patch("app.services.tool_handlers.agent_browser_tool.is_safe_url",
                   return_value=True):
            result = await _agent_browser({"action": "navigate", "url": "https://example.com"}, context=_ctx())
        assert result["success"] is True
        assert result["url"] == "https://example.com"
        assert "session_id" in result

    async def test_missing_url(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "navigate"}, context=_ctx())
        assert result["success"] is False
        assert "url is required" in result["error"]

    async def test_non_http_url(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "navigate", "url": "ftp://files.com"}, context=_ctx())
        assert result["success"] is False
        assert "http(s)" in result["error"]

    async def test_about_blank_allowed(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": ""}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "navigate", "url": "about:blank"}, context=_ctx())
        assert result["success"] is True

    async def test_ssrf_blocked(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False), \
             patch("app.services.tool_handlers.agent_browser_tool.is_safe_url", return_value=False):
            result = await _agent_browser(
                {"action": "navigate", "url": "http://169.254.169.254/"}, context=_ctx())
        assert result["success"] is False
        assert "SSRF" in result["error"]


# ── Snapshot ───────────────────────────────────────────────────────────────

class TestSnapshot:
    async def test_success(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": _SNAPSHOT_JSON}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "snapshot"}, context=_ctx())
        assert result["success"] is True
        assert result["snapshot"]["title"] == "Example Domain"

    async def test_cli_failure(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": False, "error": "No open page"}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "snapshot"}, context=_ctx())
        assert result["success"] is False
        assert "No open page" in result["error"]


# ── Act ────────────────────────────────────────────────────────────────────

class TestAct:
    async def test_click(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": "Clicked"}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser(
                {"action": "act", "verb": "click", "element": "@e12"}, context=_ctx())
        assert result["success"] is True
        assert result["verb"] == "click"

    async def test_type_with_text(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": "Typed"}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser(
                {"action": "act", "verb": "type", "element": "@e3", "text": "hello"}, context=_ctx())
        assert result["success"] is True
        assert result["typed"] == "hello"

    async def test_missing_verb(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "act", "element": "@e1"}, context=_ctx())
        assert result["success"] is False
        assert "verb" in result["error"]


# ── Extract ────────────────────────────────────────────────────────────────

class TestExtract:
    async def test_success(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": _EXTRACT_TEXT}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "extract"}, context=_ctx())
        assert result["success"] is True
        assert "Example Domain" in result["text"]
        assert result["truncated"] is False

    async def test_truncated(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        long_text = "x" * 5000
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": long_text}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False), \
             patch("app.services.tool_handlers.agent_browser_tool.settings") as s:
            s.TOOL_MAX_OUTPUT_CHARS = 100
            result = await _agent_browser({"action": "extract"}, context=_ctx())
        assert result["success"] is True
        assert len(result["text"]) == 100
        assert result["truncated"] is True


# ── Eval ───────────────────────────────────────────────────────────────────

class TestEval:
    async def test_success(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   return_value={"success": True, "stdout": "42"}), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "eval", "expression": "document.title"}, context=_ctx())
        assert result["success"] is True
        assert result["result"] == "42"

    async def test_missing_expression(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "eval"}, context=_ctx())
        assert result["success"] is False
        assert "expression" in result["error"]


# ── Close ──────────────────────────────────────────────────────────────────

class TestClose:
    async def test_success(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False), \
             patch("app.services.tool_handlers.agent_browser_tool._cleanup_session") as mc:
            result = await _agent_browser({"action": "close"}, context=_ctx())
        assert result["success"] is True
        assert result["message"] == "Browser session closed"
        mc.assert_called_once_with("conv-test-1")


# ── Unknown / default ──────────────────────────────────────────────────────

class TestUnknownAction:
    async def test_unknown(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({"action": "bogus"}, context=_ctx())
        assert result["success"] is False
        assert "Unknown action" in result["error"]

    async def test_default_is_navigate(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False):
            result = await _agent_browser({}, context=_ctx())
        assert result["success"] is False
        assert "url is required" in result["error"]


# ── Missing binary ─────────────────────────────────────────────────────────

class TestMissingBinary:
    async def test_missing_binary(self):
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        with patch("app.services.tool_handlers.agent_browser_tool.check_binaries",
                   return_value=["agent-browser"]):
            result = await _agent_browser(
                {"action": "navigate", "url": "https://example.com"}, context=_ctx())
        assert result["success"] is False


# ── _run_cli edge cases ────────────────────────────────────────────────────

class TestRunCli:
    def test_nonzero_exit(self):
        from app.services.tool_handlers.agent_browser_tool import _run_cli
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr="broke")
            result = _run_cli(["agent-browser", "open", "https://x.com"])
        assert result["success"] is False
        assert result["error"] == "broke"

    def test_timeout(self):
        import subprocess as sp
        from app.services.tool_handlers.agent_browser_tool import _run_cli
        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd=["x"], timeout=5)):
            result = _run_cli(["agent-browser", "snapshot"], timeout=5)
        assert result["success"] is False
        assert "timed out" in result["error"]


# ── Registration contract ──────────────────────────────────────────────────

class TestRegistration:
    def test_registered_in_registry(self):
        from app.services.tool_registry import registry
        assert registry.get_handler("agent_browser") is not None

    def test_handler_is_async(self):
        import asyncio
        from app.services.tool_registry import registry
        assert asyncio.iscoroutinefunction(registry.get_handler("agent_browser"))

    def test_schema_has_all_actions(self):
        from app.services.tool_handlers.agent_browser_tool import AGENT_BROWSER_SCHEMA
        f = AGENT_BROWSER_SCHEMA["function"]
        assert f["name"] == "agent_browser"
        actions = f["parameters"]["properties"]["action"]["enum"]
        for a in ("navigate", "snapshot", "act", "screenshot", "extract", "eval", "close"):
            assert a in actions


# ── execute_tool dispatch ──────────────────────────────────────────────────

class TestExecuteToolDispatch:
    async def test_dispatches_to_handler(self):
        from app.services.agent_tools import execute_tool
        from app.services.tool_registry import registry
        mock_h = AsyncMock(return_value={"success": True, "url": "https://example.com"})
        with patch.object(registry, "get_handler", return_value=mock_h), \
             patch("app.services.permissions.check_permission") as mp:
            mp.return_value = MagicMock(allowed=True, requires_confirmation=False)
            result = await execute_tool("agent_browser",
                {"action": "navigate", "url": "https://example.com"}, db=MagicMock(), context=_ctx())
        assert result["success"] is True
        assert result["url"] == "https://example.com"
        mock_h.assert_called_once()

    async def test_passes_context(self):
        from app.services.agent_tools import execute_tool
        from app.services.tool_registry import registry
        cap = {}

        async def handler(args, db, uid, *, context=None):
            cap["ctx"] = context
            return {"success": True}

        with patch.object(registry, "get_handler", return_value=handler), \
             patch("app.services.permissions.check_permission") as mp:
            mp.return_value = MagicMock(allowed=True, requires_confirmation=False)
            await execute_tool("agent_browser", {"action": "snapshot"}, db=MagicMock(),
                               context={"conversation_id": "ctx-test", "agent_name": "test"})
        assert cap["ctx"]["conversation_id"] == "ctx-test"

    async def test_unknown_tool(self):
        from app.services.agent_tools import execute_tool
        with patch("app.services.permissions.check_permission") as mp:
            mp.return_value = MagicMock(allowed=True, requires_confirmation=False)
            result = await execute_tool("no_such_tool_xyz", {}, db=MagicMock())
        assert result["success"] is False
        assert "Unknown tool" in result["error"]


# ── Full agent browsing workflow ───────────────────────────────────────────

class TestFullWorkflow:
    async def test_navigate_snapshot_extract_close(self):
        """Simulate a full agent browsing flow: navigate → snapshot → extract → close."""
        from app.services.tool_handlers.agent_browser_tool import _agent_browser
        ctx = _ctx("conv-wf-1")

        results = iter([
            {"success": True, "stdout": "Opened"},
            {"success": True, "stdout": _SNAPSHOT_JSON},
            {"success": True, "stdout": _EXTRACT_TEXT},
        ])

        with patch("app.services.tool_handlers.agent_browser_tool._run_cli",
                   side_effect=lambda *a, **kw: next(results)), \
             patch("app.services.tool_handlers.agent_browser_tool.check_binaries", return_value=False), \
             patch("app.services.tool_handlers.agent_browser_tool.is_safe_url", return_value=True), \
             patch("app.services.tool_handlers.agent_browser_tool._cleanup_session"):

            nav = await _agent_browser({"action": "navigate", "url": "https://example.com"}, context=ctx)
            assert nav["success"] is True
            assert nav["url"] == "https://example.com"

            snap = await _agent_browser({"action": "snapshot"}, context=ctx)
            assert snap["success"] is True
            assert snap["snapshot"]["title"] == "Example Domain"
            assert snap["snapshot"]["elements"][0]["role"] == "heading"

            extr = await _agent_browser({"action": "extract"}, context=ctx)
            assert extr["success"] is True
            assert "Example Domain" in extr["text"]

            closed = await _agent_browser({"action": "close"}, context=ctx)
            assert closed["success"] is True
