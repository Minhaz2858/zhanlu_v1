"""Tests for the 3-layer tool result persistence system.

Layer 1: per-tool cap (truncation) -- already exists in tool_security.py.
Layer 2: per-result persistence to disk + preview replacement.
Layer 3: per-turn aggregate budget (spill largest results to disk).
"""
import json
import os
import sys
import tempfile

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_result_persistence import (
    PersistenceConfig,
    persist_tool_result,
    apply_turn_budget,
    budget_for_context_window,
)


def test_small_result_not_persisted():
    """Results under the per-result threshold stay inline -- no disk write."""
    config = PersistenceConfig(result_threshold_chars=1000, preview_chars=200)
    with tempfile.TemporaryDirectory() as tmpdir:
        result_str = json.dumps({"success": True, "content": "small result"})
        new_str, meta = persist_tool_result(
            "web_search", result_str, tmpdir, config
        )
        assert new_str == result_str  # unchanged
        assert meta["persisted"] is False


def test_large_result_persisted_with_preview():
    """Results over the threshold get written to disk; inline preview replaces them."""
    config = PersistenceConfig(result_threshold_chars=100, preview_chars=50)
    with tempfile.TemporaryDirectory() as tmpdir:
        big_content = "X" * 500
        result_str = json.dumps({"success": True, "content": big_content})
        new_str, meta = persist_tool_result(
            "web_search", result_str, tmpdir, config
        )
        assert meta["persisted"] is True
        assert "stored_path" in meta
        assert os.path.exists(meta["stored_path"])
        assert len(new_str) < len(result_str)  # preview is smaller
        # The preview should tell the LLM where to find the full result
        assert "stored_path" not in new_str  # meta key not in content
        assert meta["stored_path"] in new_str or "read_file" in new_str


def test_read_file_never_persisted():
    """read_file results are never persisted (prevents persist->read->persist loops)."""
    config = PersistenceConfig(result_threshold_chars=10, preview_chars=5)
    with tempfile.TemporaryDirectory() as tmpdir:
        result_str = json.dumps({"success": True, "content": "X" * 500})
        new_str, meta = persist_tool_result(
            "read_file", result_str, tmpdir, config
        )
        assert meta["persisted"] is False
        assert new_str == result_str


def test_turn_budget_spills_largest_results():
    """When total turn output exceeds the budget, largest results get persisted."""
    config = PersistenceConfig(
        result_threshold_chars=100000,  # high so Layer 2 doesn't fire first
        preview_chars=100,
        turn_budget_chars=2000,  # low to trigger Layer 3
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        results = [
            ("web_search", json.dumps({"success": True, "content": "A" * 2000})),
            ("web_extract", json.dumps({"success": True, "content": "B" * 1500})),
            ("execute_code", json.dumps({"success": True, "content": "C" * 1000})),
        ]
        spilled = apply_turn_budget(results, tmpdir, config)
        # Total ~4600 chars, budget 2000, so the largest should be spilled
        total_inline = sum(len(r) for _, r in spilled)
        assert total_inline < 4600  # at least some reduction
        # The largest result (web_search) should have been spilled to disk
        web_result = [r for name, r in spilled if name == "web_search"][0]
        assert "truncated" in web_result.lower() or len(web_result) < 2000


def test_turn_budget_no_spill_when_under_budget():
    """When total turn output is under the budget, nothing is spilled."""
    config = PersistenceConfig(
        result_threshold_chars=10000,  # high so Layer 2 doesn't fire
        turn_budget_chars=10000,  # high so Layer 3 doesn't fire
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        results = [
            ("web_search", json.dumps({"success": True, "content": "A" * 400})),
            ("web_extract", json.dumps({"success": True, "content": "B" * 300})),
        ]
        spilled = apply_turn_budget(results, tmpdir, config)
        assert spilled == list(results)  # unchanged


def test_budget_for_context_window_scales_down():
    """A small context window produces a smaller budget than the default."""
    small = budget_for_context_window(8192)  # 8K tokens
    large = budget_for_context_window(200000)  # 200K tokens
    # Small model should have smaller thresholds (floored)
    assert small.result_threshold_chars <= large.result_threshold_chars
    assert small.turn_budget_chars <= large.turn_budget_chars
    # But never below the floor
    assert small.result_threshold_chars >= 4000
    assert small.turn_budget_chars >= 16000


def test_budget_for_context_window_none_returns_default():
    """None or 0 context length returns the default config."""
    default = PersistenceConfig()
    none_budget = budget_for_context_window(None)
    zero_budget = budget_for_context_window(0)
    assert none_budget.result_threshold_chars == default.result_threshold_chars
    assert zero_budget.result_threshold_chars == default.result_threshold_chars
