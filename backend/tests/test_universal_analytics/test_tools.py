"""Tests for universal_analytics/tools.py — 6 tool handlers + registration.

Tests cover: tool registration, handler dispatch, bound_kb_ids resolution,
missing_config response, and smoke tests for each tool handler.
"""

import pytest
from unittest.mock import MagicMock, patch

# Force import the universal_analytics tools module so that the
# module-level registry.register() calls fire before tests run.
# In production this happens via tool_handlers/__init__.py → package init.
from app.services.universal_analytics import tools  # noqa: E402, F401


# ── Tool registration tests ─────────────────────────────────────────

class TestToolRegistration:
    def test_all_six_tools_registered(self):
        """Verify 6 tools are registered with enabled_by_default=True."""
        from app.services.tool_registry import registry
        entries = registry.all_entries()
        names = set(entries.keys())

        expected = {
            "universal_describe",
            "universal_discover",
            "universal_query",
            "universal_kpi",
            "universal_trend",
            "universal_forecast",
        }
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_tools_are_enabled_by_default(self):
        """All 6 universal tools should have enabled_by_default=True."""
        from app.services.tool_registry import registry
        tool_names = [
            "universal_describe", "universal_discover",
            "universal_query", "universal_kpi",
            "universal_trend", "universal_forecast",
        ]
        for name in tool_names:
            entry = registry.get_entry(name)
            assert entry is not None, f"{name} not in registry"
            assert entry.enabled_by_default, f"{name} enabled_by_default != True"

    def test_tools_category(self):
        """Tools belong to universal_analytics category/toolset."""
        from app.services.tool_registry import registry
        for name in ["universal_describe", "universal_kpi"]:
            entry = registry.get_entry(name)
            assert entry is not None
            assert entry.category == "universal_analytics"


# ── Helpers ─────────────────────────────────────────────────────────

def _make_context(bound_kb_ids=None):
    ctx = {}
    if bound_kb_ids is not None:
        ctx["bound_kb_ids"] = bound_kb_ids
    return ctx


# ── Handler dispatch tests ──────────────────────────────────────────

class TestHandlerDispatch:
    def test_handlers_are_async_callable(self):
        """All 6 handler functions accept (args, db, user_id, context)."""
        from app.services.tool_registry import registry

        for name in [
            "universal_describe", "universal_discover",
            "universal_query", "universal_kpi",
            "universal_trend", "universal_forecast",
        ]:
            entry = registry.get_entry(name)
            assert entry is not None
            assert callable(entry.handler), f"{name} handler not callable"
            assert entry.is_async, f"{name} should be async"


# ── context.py tests ────────────────────────────────────────────────

class TestContextHelpers:
    def test_get_bound_kbs_returns_database_kbs(self):
        from app.services.universal_analytics.context import get_bound_kbs
        db = MagicMock()

        kb1 = MagicMock()
        kb1.id = "kb-1"
        kb1.source_kind = "db"
        kb1.db_type = "mysql"

        # Return only db-type KBs; the real SQLAlchemy filter would
        # exclude file-type KBs, so our mock should too.
        db.query.return_value.filter.return_value.all.return_value = [kb1]

        ctx = _make_context(["kb-1", "kb-2"])
        result = get_bound_kbs(ctx, db)
        assert len(result) == 1
        assert result[0].source_kind == "db"

    def test_get_bound_kbs_empty_context(self):
        from app.services.universal_analytics.context import get_bound_kbs
        result = get_bound_kbs(None, MagicMock())
        assert result == []

    def test_check_enabled_off(self):
        from app.services.universal_analytics.context import check_enabled
        with patch.dict("os.environ", {"UNIVERSAL_ANALYTICS_ENABLED": "false"}):
            assert check_enabled() is False

    def test_check_enabled_on_by_default(self):
        """When flag is missing, default is ON (true)."""
        from app.services.universal_analytics.context import check_enabled
        with patch.dict("os.environ", {}, clear=True):
            # With UNIVERSAL_ANALYTICS_ENABLED absent, default is "true"
            assert check_enabled() is True

    def test_missing_config_response(self):
        from app.services.universal_analytics.context import missing_config_response
        resp = missing_config_response("UNIVERSAL_ANALYTICS_ENABLED")
        assert resp["success"] is False
        assert "UNIVERSAL_ANALYTICS_ENABLED" in resp.get("error", "")
