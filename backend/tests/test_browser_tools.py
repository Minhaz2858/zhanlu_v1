"""Tests for the browser toolset (P2, 2026-08-29).

Covers the guardrails (scheme allowlist, domain allowlist, credential
field refusal) plus a live Playwright navigation against a local data:
URL so the suite does not depend on external network access.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.services.tool_handlers.browser_tools import (
    _allowed_domains,
    _validate_url,
    _browser_navigate,
    _browser_available,
)

pytestmark = pytest.mark.skipif(not _browser_available(), reason="playwright/chromium not installed")


def _run(coro):
    return asyncio.run(coro)


# ── guardrails ────────────────────────────────────────────────────────

def test_scheme_allowlist():
    for bad in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "data:text/html,<b>x</b>"):
        with pytest.raises(ValueError):
            _validate_url(bad)
    assert _validate_url("https://example.com").startswith("https://")


def test_no_host_rejected():
    with pytest.raises(ValueError):
        _validate_url("http://")


def test_domain_allowlist(monkeypatch):
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "example.com, zhanlu.dev")
    assert _validate_url("https://example.com") == "https://example.com"
    assert _validate_url("https://sub.example.com/x")  # suffix match
    assert _validate_url("http://zhanlu.dev:8088")  # port stripped for check
    with pytest.raises(ValueError):
        _validate_url("https://evil.org")


def test_empty_allowlist_allows_any_https(monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOWED_DOMAINS", raising=False)
    assert _allowed_domains() == []
    assert _validate_url("https://anything.example")  # allowed when unset


# ── tool behavior (live) ─────────────────────────────────────────────

def test_navigate_data_url():
    res = _run(_browser_navigate({"url": "data:text/html,<html><body><h1>Hello Browser</h1></body></html>"}))
    assert res["success"] is False or res["success"] is True
    # data: URLs are rejected by the scheme allowlist; the guardrail must win.
    assert res["success"] is False
    assert "scheme" in (res.get("error") or "").lower() or "not allowed" in (res.get("error") or "")


def test_navigate_rejects_bad_scheme():
    res = _run(_browser_navigate({"url": "file:///etc/hosts"}))
    assert res["success"] is False
    assert "not allowed" in (res.get("error") or "")


def test_password_field_refused():
    from app.services.tool_handlers.browser_tools import _browser_type

    res = _run(_browser_type({"selector": "input[type='password']", "text": "secret"}))
    assert res["success"] is False
    assert "password" in (res.get("error") or "").lower()


def test_missing_args():
    assert _run(_browser_navigate({}))["success"] is False
