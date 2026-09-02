"""Tests for the ``computer_use`` tool — the managed-browser facade.

Acceptance (design doc ``docs/plans/2026-09-02-computer-use-design.md``):
  - open → screenshot → get_state → click @eN → type → close round-trip
  - unauthorized URLs blocked (SSRF guard + ``COMPUTER_USE_ALLOWED_URLS``)
  - per-conversation step budget enforced; ``close`` resets it
  - ``COMPUTER_USE_ENABLED=false`` returns the legacy "not implemented" stub
  - ``is_url_in_allowlist`` host/subdomain/URL-prefix semantics
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings  # noqa: E402

_SNAPSHOT_JSON = json.dumps({
    "url": "https://example.com",
    "title": "Example Domain",
    "elements": [
        {"role": "heading", "name": "Example Domain", "ref": "@e1"},
        {"role": "textbox", "name": "Search", "ref": "@e2"},
    ],
})


def _ctx(cid="cu-conv-1"):
    return {"conversation_id": cid}


class TestComputerUseWorkflow:
    @pytest.mark.asyncio
    async def test_open_screenshot_state_click_type_close(self):
        from app.services.tool_handlers.computer_use_tool import _computer_use
        results = iter([
            {"success": True, "stdout": "Opened"},
            {"success": True, "stdout": ""},  # screenshot
            {"success": True, "stdout": _SNAPSHOT_JSON},
            {"success": True, "stdout": "Clicked @e2"},
            {"success": True, "stdout": "Typed"},
        ])
        with patch.object(settings, "COMPUTER_USE_ENABLED", True), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False), \
             patch("app.services.computer_use.manager.run_cli",
                   side_effect=lambda *a, **kw: next(results)), \
             patch("app.services.computer_use.manager.is_safe_url",
                   return_value=True), \
             patch("app.services.computer_use.manager.is_url_in_allowlist",
                   return_value=True):
            ctx = _ctx("cu-wf-1")
            opened = await _computer_use(
                {"action": "open", "url": "https://example.com"}, context=ctx)
            assert opened["success"] is True
            assert opened["url"] == "https://example.com"
            assert "session_id" in opened

            shot = await _computer_use({"action": "screenshot"}, context=ctx)
            assert shot["success"] is True
            assert shot["file_path"].endswith(".png")

            state = await _computer_use({"action": "get_state"}, context=ctx)
            assert state["success"] is True
            assert state["state"]["title"] == "Example Domain"
            assert state["state"]["elements"][1]["ref"] == "@e2"

            clicked = await _computer_use(
                {"action": "click", "element": "@e2"}, context=ctx)
            assert clicked["success"] is True
            assert clicked["clicked"] == "@e2"

            typed = await _computer_use(
                {"action": "type", "element": "@e2", "text": "hello"}, context=ctx)
            assert typed["success"] is True
            assert typed["typed"] == "hello"

            closed = await _computer_use({"action": "close"}, context=ctx)
            assert closed["success"] is True
            assert closed["message"] == "Browser session closed"

    @pytest.mark.asyncio
    async def test_click_and_type_require_element(self):
        from app.services.tool_handlers.computer_use_tool import _computer_use
        with patch.object(settings, "COMPUTER_USE_ENABLED", True), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False), \
             patch("app.services.computer_use.manager.run_cli",
                   return_value={"success": True, "stdout": ""}):
            ctx = _ctx("cu-args-1")
            r = await _computer_use({"action": "click"}, context=ctx)
            assert r["success"] is False
            assert "element is required" in r["error"]

            r = await _computer_use(
                {"action": "type", "element": "@e2"}, context=ctx)
            assert r["success"] is False
            assert "text is required" in r["error"]


class TestComputerUseBlocking:
    @pytest.mark.asyncio
    async def test_ssrf_blocked(self):
        from app.services.tool_handlers.computer_use_tool import _computer_use
        with patch.object(settings, "COMPUTER_USE_ENABLED", True), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False), \
             patch("app.services.computer_use.manager.is_safe_url",
                   return_value=False):
            result = await _computer_use(
                {"action": "open", "url": "http://169.254.169.254/latest/meta-data"},
                context=_ctx("cu-ssrf-1"))
        assert result["success"] is False
        assert "SSRF" in result["error"]

    @pytest.mark.asyncio
    async def test_allowlist_blocked(self):
        from app.services.tool_handlers.computer_use_tool import _computer_use
        with patch.object(settings, "COMPUTER_USE_ENABLED", True), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False), \
             patch("app.services.computer_use.manager.is_safe_url",
                   return_value=True), \
             patch("app.services.computer_use.manager.is_url_in_allowlist",
                   return_value=False):
            result = await _computer_use(
                {"action": "open", "url": "https://evil.example.com"},
                context=_ctx("cu-al-1"))
        assert result["success"] is False
        assert "allowlist" in result["error"]

    @pytest.mark.asyncio
    async def test_non_http_url_rejected(self):
        from app.services.tool_handlers.computer_use_tool import _computer_use
        with patch.object(settings, "COMPUTER_USE_ENABLED", True), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False):
            result = await _computer_use(
                {"action": "open", "url": "file:///etc/passwd"},
                context=_ctx("cu-url-1"))
        assert result["success"] is False
        assert "http(s)" in result["error"]


class TestStepBudget:
    @pytest.mark.asyncio
    async def test_exhausted_then_close_resets(self):
        from app.services.tool_handlers.computer_use_tool import _computer_use
        with patch.object(settings, "COMPUTER_USE_ENABLED", True), \
             patch.object(settings, "COMPUTER_USE_MAX_STEPS", 3), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False), \
             patch("app.services.computer_use.manager.run_cli",
                   return_value={"success": True, "stdout": ""}), \
             patch("app.services.computer_use.manager.is_safe_url",
                   return_value=True), \
             patch("app.services.computer_use.manager.is_url_in_allowlist",
                   return_value=True):
            ctx = _ctx("cu-cap-1")
            for action in ("open", "screenshot", "get_state"):
                r = await _computer_use(
                    {"action": action, "url": "https://example.com"}, context=ctx)
                assert r["success"] is True

            r = await _computer_use(
                {"action": "click", "element": "@e2"}, context=ctx)
            assert r["success"] is False
            assert "budget" in r["error"].lower()

            r = await _computer_use({"action": "close"}, context=ctx)
            assert r["success"] is True

            r = await _computer_use(
                {"action": "click", "element": "@e2"}, context=ctx)
            assert r["success"] is True


class TestDisabledFlag:
    @pytest.mark.asyncio
    async def test_disabled_returns_legacy_stub(self):
        from app.services.tool_handlers.computer_use_tool import _computer_use
        with patch.object(settings, "COMPUTER_USE_ENABLED", False), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False):
            result = await _computer_use(
                {"action": "screenshot"}, context=_ctx("cu-off-1"))
        assert result["success"] is False
        assert "not yet implemented" in result["error"]

        # Any action (even a valid dispatch target) hits the stub first.
        with patch.object(settings, "COMPUTER_USE_ENABLED", False), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False):
            result2 = await _computer_use(
                {"action": "open", "url": "https://example.com"},
                context=_ctx("cu-off-2"))
        assert "not yet implemented" in result2["error"]

    @pytest.mark.asyncio
    async def test_unknown_action_when_enabled(self):
        """eval is intentionally not exposed by the facade."""
        from app.services.tool_handlers.computer_use_tool import _computer_use
        with patch.object(settings, "COMPUTER_USE_ENABLED", True), \
             patch("app.services.tool_handlers.computer_use_tool.check_binaries",
                   return_value=False):
            result = await _computer_use(
                {"action": "eval", "expression": "1+1"}, context=_ctx("cu-unk-1"))
        assert result["success"] is False
        assert "Unknown computer_use action" in result["error"]


class TestUrlAllowlist:
    def test_empty_allowlist_allows_all(self):
        from app.services.tool_security import is_url_in_allowlist
        assert is_url_in_allowlist("https://example.com", []) is True
        assert is_url_in_allowlist("https://example.com", None) is True

    def test_host_match_includes_subdomains(self):
        from app.services.tool_security import is_url_in_allowlist
        assert is_url_in_allowlist("https://example.com/page", ["example.com"]) is True
        assert is_url_in_allowlist("https://docs.example.com/a", ["example.com"]) is True
        assert is_url_in_allowlist("https://notexample.com", ["example.com"]) is False
        assert is_url_in_allowlist("https://evil-example.com", ["example.com"]) is False

    def test_scheme_pattern_matches_url_prefix(self):
        from app.services.tool_security import is_url_in_allowlist
        assert is_url_in_allowlist("https://example.com/a/b", ["https://example.com/a"]) is True
        assert is_url_in_allowlist("http://example.com/a/b", ["https://example.com/a"]) is False

    def test_wildcard_host_pattern(self):
        from app.services.tool_security import is_url_in_allowlist
        assert is_url_in_allowlist("https://a.example.com", ["*.example.com"]) is True
        assert is_url_in_allowlist("https://example.com", ["*.example.com"]) is False


class TestRegistration:
    def test_registered_and_schema_actions(self):
        from app.services.tool_registry import registry
        from app.services.tool_handlers.computer_use_tool import COMPUTER_USE_SCHEMA
        assert registry.get_handler("computer_use") is not None
        f = COMPUTER_USE_SCHEMA["function"]
        assert f["name"] == "computer_use"
        actions = f["parameters"]["properties"]["action"]["enum"]
        for a in ("open", "screenshot", "get_state", "click", "type", "scroll", "close"):
            assert a in actions
        # No pixel coordinates / arbitrary JS in the facade.
        assert "x" not in f["parameters"]["properties"]
        assert "y" not in f["parameters"]["properties"]
        assert "eval" not in actions
