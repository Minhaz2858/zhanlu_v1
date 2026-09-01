"""Tests for the ui-ux-pro-max tool handler.

Verifies:
* Subprocess invocation shape (correct CLI argv, correct flags).
* Validation of domain + stack enums.
* Error handling: timeout, non-zero exit, missing CLI.
* Required args.
* Result wrapping (success / failure keys).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


def _run(coro):
    """Run an async tool handler to completion.

    Uses ``asyncio.run()`` (Python 3.7+) which creates and tears down a fresh
    event loop per call. This is safer than ``asyncio.get_event_loop()`` in
    parallel pytest workers (xdist) where no current loop is guaranteed.
    """
    return asyncio.run(coro)


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Build a mock CompletedProcess."""
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


def _pretend_cli_present(monkeypatch, t):
    original = t.Path.is_file

    def fake_is_file(path):
        if path == t._SEARCH_PY:
            return True
        return original(path)

    monkeypatch.setattr(t.Path, "is_file", fake_is_file)


# ── uiux_search ─────────────────────────────────────────────────────────


def test_search_calls_cli_with_query_and_domain(monkeypatch):
    """Search passes --domain and the query to the upstream CLI."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return _proc(stdout="## UI Pro Max\nResult 1")

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = _run(t._uiux_search({
        "query": "monthly sales",
        "domain": "chart",
        "stack": "html-tailwind",
        "max_results": 3,
    }))

    assert result["success"] is True
    assert "monthly sales" in " ".join(captured["cmd"])
    assert "--domain" in captured["cmd"]
    assert "chart" in captured["cmd"]
    assert "--stack" in captured["cmd"]
    assert "html-tailwind" in captured["cmd"]
    assert "--max-results" in captured["cmd"]
    assert "3" in captured["cmd"]
    assert captured["timeout"] == t._TIMEOUT_SECONDS
    # Result must echo back the query metadata
    assert result["domain"] == "chart"
    assert result["stack"] == "html-tailwind"


def test_search_omits_optional_flags_when_not_provided(monkeypatch):
    """Search with only the query does NOT add empty --domain/--stack flags."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _proc(stdout="ok")

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = _run(t._uiux_search({"query": "dashboard palette"}))

    assert result["success"] is True
    assert "--domain" not in captured["cmd"]
    assert "--stack" not in captured["cmd"]
    assert "dashboard palette" in " ".join(captured["cmd"])


def test_search_rejects_empty_query():
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    result = _run(t._uiux_search({"query": ""}))
    assert result["success"] is False
    assert "query" in result["error"].lower()


def test_search_rejects_invalid_domain():
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    result = _run(t._uiux_search({"query": "x", "domain": "bogus"}))
    assert result["success"] is False
    assert "invalid domain" in result["error"]


def test_search_rejects_invalid_stack():
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    result = _run(t._uiux_search({"query": "x", "stack": "no-such-stack"}))
    assert result["success"] is False
    assert "invalid stack" in result["error"]


def test_search_handles_timeout(monkeypatch):
    """A subprocess.TimeoutExpired returns graceful fallback, never raises."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 30))

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = _run(t._uiux_search({"query": "x"}))
    assert result["success"] is False
    assert "timeout" in result["error"].lower()


def test_search_handles_nonzero_exit(monkeypatch):
    """Non-zero returncode returns success=False with stderr surfaced."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    def fake_run(cmd, **kwargs):
        return _proc(stdout="partial", stderr="bad arg", returncode=2)

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = _run(t._uiux_search({"query": "x"}))
    assert result["success"] is False
    assert result.get("returncode") == 2
    assert "bad arg" in result["error"]


def test_search_clamps_max_results(monkeypatch):
    """max_results above 5 clamps to 5; below 1 clamps to 1."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return _proc(stdout="ok")

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    # Above max → "5"
    _run(t._uiux_search({"query": "x", "max_results": 99}))
    assert "5" in captured[-1]
    # Below min → "1"
    _run(t._uiux_search({"query": "x", "max_results": 0}))
    assert "1" in captured[-1]
    # Non-integer falls back to 3
    _run(t._uiux_search({"query": "x", "max_results": "abc"}))
    assert "3" in captured[-1]


# ── uiux_design_system ─────────────────────────────────────────────────


def test_design_system_calls_with_flag(monkeypatch):
    """Design-system mode prepends --design-system flag."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _proc(stdout="# Design System Spec\nPalette: ...")

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = _run(t._uiux_design_system({
        "query": "ERP financial dashboard",
        "project": "ProjectAlpha",
        "density": 8,
        "variance": 3,
        "motion": 5,
    }))

    assert result["success"] is True
    assert "--design-system" in captured["cmd"]
    assert "ERP financial dashboard" in " ".join(captured["cmd"])
    assert "-p" in captured["cmd"]
    assert "ProjectAlpha" in captured["cmd"]
    assert "--density" in captured["cmd"]
    assert "8" in captured["cmd"]
    assert result["dials"]["density"] == 8
    assert result["dials"]["variance"] == 3
    assert result["dials"]["motion"] == 5


def test_design_system_omits_unset_dials(monkeypatch):
    """Unset dials are NOT sent to the CLI."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _proc(stdout="ok")

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = _run(t._uiux_design_system({"query": "x"}))

    assert result["success"] is True
    assert "--variance" not in captured["cmd"]
    assert "--motion" not in captured["cmd"]
    assert "--density" not in captured["cmd"]
    assert "dials" in result and result["dials"] == {}


def test_design_system_clamps_dials(monkeypatch):
    """Dials clamp to [1, 10] range."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _proc(stdout="ok")

    _pretend_cli_present(monkeypatch, t)
    monkeypatch.setattr("subprocess.run", fake_run)

    _run(t._uiux_design_system({"query": "x", "density": 999}))
    assert "10" in captured["cmd"]

    _run(t._uiux_design_system({"query": "x", "variance": -5}))
    assert "1" in captured["cmd"]


def test_design_system_rejects_empty_query():
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    result = _run(t._uiux_design_system({"query": ""}))
    assert result["success"] is False


def test_search_uses_builtin_fallback_when_cli_missing(monkeypatch):
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    monkeypatch.setattr(t, "_SEARCH_PY", t.Path("/nonexistent/search.py"))

    result = _run(t._uiux_search({"query": "sales dashboard", "domain": "chart"}))

    assert result["success"] is True
    assert result["fallback_used"] is True
    assert "dashboard" in result["result"].lower()


def test_design_system_uses_builtin_fallback_when_cli_missing(monkeypatch):
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    monkeypatch.setattr(t, "_SEARCH_PY", t.Path("/nonexistent/search.py"))

    result = _run(t._uiux_design_system({"query": "sales dashboard", "density": 8}))

    assert result["success"] is True
    assert result["fallback_used"] is True
    assert "kpi" in result["result"].lower()


# ── Registration / check_fn ────────────────────────────────────────────


def test_both_tools_registered():
    """Both tools appear in the registry catalog."""
    # Force-import the tool_handlers package so side-effect registration
    # runs (paranoia: parallel pytest workers don't share module state).
    import app.services.tool_handlers  # noqa: F401
    from app.services.tool_registry import registry

    available = set(registry.list_available())
    assert "uiux_search" in available, f"uiux_search not in: {sorted(available)[:20]}"
    assert "uiux_design_system" in available, f"uiux_design_system not in: {sorted(available)[:20]}"


def test_check_fn_passes_when_cli_present(monkeypatch):
    """_uiux_check returns True when search.py exists."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    _pretend_cli_present(monkeypatch, t)
    assert t._uiux_check() is True


def test_check_fn_returns_false_when_cli_missing(monkeypatch):
    """_uiux_check returns False when search.py is missing."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    monkeypatch.setattr(t, "_SEARCH_PY", t.Path("/nonexistent/search.py"))
    assert t._uiux_check() is False


def test_schemas_have_required_fields():
    """Both tool schemas have the function.name and required query param."""
    from app.services.tool_handlers import ui_ux_pro_max_tool as t

    for schema in (t.UIUX_SEARCH_SCHEMA, t.UIUX_DESIGN_SYSTEM_SCHEMA):
        assert schema["function"]["name"].startswith("uiux_")
        params = schema["function"]["parameters"]
        assert "query" in params["properties"]
        assert "query" in params["required"]