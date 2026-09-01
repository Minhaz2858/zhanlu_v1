"""Tests for the artifact-coverage gate (artifact_tool._ppt_coverage_gate).

Covers:
  - Gate OFF (default): always returns None — no regression.
  - Gate ON + thin payload: returns insufficient_coverage error.
  - Gate ON + rich payload (>= MIN_DIMENSIONS): returns None.
  - Gate ON + soft-block: logs a warning + returns None (allows render).
  - Gate ON + bypass: returns None unconditionally.
  - Gate ON + payload missing coverage_dimensions: returns None
    (fail-open — non-market decks should pass through untouched).
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest


# We import the gate helper directly.  ``artifact_tool`` imports a lot of
# modules at module load time, but the helper function is well-isolated.
from app.services.tool_handlers.artifact_tool import _ppt_coverage_gate


# Stub logger so we don't pollute pytest output.
class _StubLogger:
    def __init__(self):
        self.messages = []
    def info(self, msg, *args):
        self.messages.append(("info", msg % args if args else msg))
    def warning(self, msg, *args):
        self.messages.append(("warning", msg % args if args else msg))
    def debug(self, msg, *args):
        self.messages.append(("debug", msg % args if args else msg))


@pytest.fixture
def stub_log():
    return _StubLogger()


@pytest.fixture(autouse=True)
def _save_and_restore_settings():
    """Back up settings flags before each test and restore after."""
    from app.config import settings
    saved = {
        k: getattr(settings, k)
        for k in [
            "COMPREHENSIVE_DATA_GATE_ENABLED",
            "COMPREHENSIVE_DATA_GATE_SOFT_BLOCK",
            "COMPREHENSIVE_DATA_GATE_BYPASS",
            "COMPREHENSIVE_DATA_MIN_DIMENSIONS",
        ]
    }
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


# ---------------------------------------------------------------------------
# Gate OFF (default behavior — zero regression)
# ---------------------------------------------------------------------------
class TestGateOffByDefault:
    def test_returns_none_when_disabled(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = False
        settings.COMPREHENSIVE_DATA_GATE_BYPASS = False
        payload = {"coverage_dimensions": []}  # empty — would fail if enabled
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is None  # no error, render proceeds

    def test_returns_none_when_bypass_on(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_GATE_BYPASS = True
        payload = {"coverage_dimensions": []}
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is None  # bypass wins


# ---------------------------------------------------------------------------
# Gate ON + thin payload
# ---------------------------------------------------------------------------
class TestGateBlocksThinPayload:
    def test_zero_dimensions_hard_fail(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_GATE_BYPASS = False
        settings.COMPREHENSIVE_DATA_GATE_SOFT_BLOCK = False
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        payload = {"coverage_dimensions": []}
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is not None
        assert result["success"] is False
        assert result["reason"] == "insufficient_coverage"
        assert result["coverage_count"] == 0
        assert "missing_dimensions" in result
        # All 8 dimensions should be missing
        assert len(result["missing_dimensions"]) == 8

    def test_one_dimension_hard_fail(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_GATE_SOFT_BLOCK = False
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        payload = {
            "coverage_dimensions": ["core_metrics"],
        }
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is not None
        assert result["reason"] == "insufficient_coverage"
        assert result["coverage_count"] == 1
        assert result["min_dimensions"] == 3
        assert "core_metrics" not in result["missing_dimensions"]


# ---------------------------------------------------------------------------
# Gate ON + rich payload (passes)
# ---------------------------------------------------------------------------
class TestGateAllowsRichPayload:
    def test_three_dimensions_passes_at_default_min(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_GATE_BYPASS = False
        settings.COMPREHENSIVE_DATA_GATE_SOFT_BLOCK = False
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        payload = {
            "coverage_dimensions": ["core_metrics", "supply_side", "demand_side"],
        }
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is None  # allowed

    def test_eight_dimensions_passes(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        payload = {
            "coverage_dimensions": [
                "core_metrics", "historical_trends", "cost_structure",
                "supply_side", "demand_side", "macro_context",
                "forward_indicators", "cross_segment_relationships",
            ]
        }
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is None


# ---------------------------------------------------------------------------
# Soft-block path
# ---------------------------------------------------------------------------
class TestGateSoftBlock:
    def test_soft_block_returns_none_with_warning(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_GATE_SOFT_BLOCK = True
        settings.COMPREHENSIVE_DATA_GATE_BYPASS = False
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        payload = {"coverage_dimensions": ["core_metrics"]}  # 1 < 3
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is None  # soft-block allows
        # Gate emits a WARNING to the supplied logger (caplog doesn't capture
        # the stub_logger's calls; assert against the stub_log directly).
        warnings = [m for lvl, m in stub_log.messages if lvl == "warning"]
        assert any("SOFT-BLOCK" in m for m in warnings), warnings


# ---------------------------------------------------------------------------
# Fail-open: no coverage_dimensions in payload → pass through
# ---------------------------------------------------------------------------
class TestGateFailOpenWithoutCoverage:
    def test_payload_without_coverage_dimensions_passes(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_GATE_BYPASS = False
        settings.COMPREHENSIVE_DATA_GATE_SOFT_BLOCK = False
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        # Payload from a non-market deck (pitch deck, design-heavy HTML deck)
        # won't carry coverage_dimensions at all. The gate is fail-open here
        # so non-market decks keep working.
        payload = {"title": "Some other deck", "charts": []}
        result = _ppt_coverage_gate(payload, stub_log)
        assert result is None

    def test_empty_payload_passes(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        result = _ppt_coverage_gate({}, stub_log)
        assert result is None

    def test_non_list_coverage_dimensions_passes(self, stub_log):
        from app.config import settings
        settings.COMPREHENSIVE_DATA_GATE_ENABLED = True
        settings.COMPREHENSIVE_DATA_MIN_DIMENSIONS = 3
        # ``coverage_dimensions`` accidentally set to a string — fail-open
        # so the deck still renders (vs. crashing here).
        result = _ppt_coverage_gate({"coverage_dimensions": "broken"}, stub_log)
        assert result is None
