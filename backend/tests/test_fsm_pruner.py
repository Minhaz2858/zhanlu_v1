"""Tests for the Hermes-style proactive FSM-state pruning.

The fix: between FSM iterations in the v3 stream loop, replace old tool
results with a compact state checkpoint. This prevents accumulation of
50k+ tokens of tool results across multiple data fetches.

Inspired by Hermes Agent's ``proactive_prune_tokens`` — deterministic,
no-LLM, sub-millisecond.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.agent_loop.fsm_pruner import (
    prune_between_fsm_states,
    hard_cap_tool_result,
    FSM_PRUNE_MIN_RESULT_CHARS,
    FSM_PRUNE_MAX_TOTAL_CHARS,
)


def _tool_msg(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _asst_msg() -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "tc-1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    ]}


# ── Hard cap at source (Hermes Step 1) ──────────────────────────────────

class TestHardCapToolResult:

    def test_small_result_unchanged(self):
        result = {"rows": [{"id": 1, "name": "test"}]}
        out = hard_cap_tool_result(result)
        assert out == result

    def test_large_result_capped(self):
        # Build a 60k-char result (over the 50k cap)
        rows = [{"id": i, "data": "X" * 1000} for i in range(60)]
        result = {"rows": rows, "total": len(rows)}
        out = hard_cap_tool_result(result)
        # Must be capped to ~50k chars
        out_str = str(out)
        assert len(out_str) < 60_000, f"result should be capped: {len(out_str)}"
        # And must contain truncation marker
        assert "truncated" in out_str.lower() or "capped" in out_str.lower()

    def test_preserves_structure_when_capped(self):
        rows = [{"id": i, "name": f"item_{i}"} for i in range(1000)]
        result = {"rows": rows, "total": len(rows)}
        out = hard_cap_tool_result(result)
        # Must keep the structure (rows key exists, even if truncated)
        assert "rows" in out or "truncated" in str(out).lower()


# ── Proactive pruning between FSM states (Hermes Step 2) ────────────────

class TestPruneBetweenFSMStates:

    def test_small_conversation_unchanged(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
        ]
        out = prune_between_fsm_states(messages, current_state="act")
        assert out == messages

    def test_replaces_old_tool_results_with_checkpoint(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "show data"},
            _asst_msg(),
            _tool_msg("tc-1", "X" * 25000),  # old, large result
            _asst_msg(),
            _tool_msg("tc-2", "Y" * 25000),  # old, large result
            _asst_msg(),
            _tool_msg("tc-3", "Z" * 5000),   # latest, keep this
        ]
        out = prune_between_fsm_states(messages, current_state="observe")
        # Should have fewer messages
        assert len(out) < len(messages)
        # The old large results should be replaced with a checkpoint
        has_checkpoint = any(
            m.get("role") == "assistant" and "checkpoint" in str(m.get("content", "")).lower()
            for m in out
        )
        assert has_checkpoint, "should have a checkpoint message"
        # The latest tool result should be preserved
        latest_tool = next(m for m in out if m.get("tool_call_id") == "tc-3")
        assert latest_tool["content"] == "Z" * 5000

    def test_checkpoint_contains_metadata(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "show data"},
            _asst_msg(),
            _tool_msg("tc-1", "X" * 25000),
            _asst_msg(),
            _tool_msg("tc-2", "Y" * 25000),
            _asst_msg(),
            _tool_msg("tc-3", "Z" * 5000),
        ]
        out = prune_between_fsm_states(messages, current_state="observe")
        checkpoint = next(
            m for m in out
            if m.get("role") == "assistant" and "checkpoint" in str(m.get("content", "")).lower()
        )
        content = checkpoint.get("content", "")
        # Should mention how many tool results were replaced
        assert "2" in content or "two" in content.lower() or "multiple" in content.lower()

    def test_total_size_reduced_significantly(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "show data"},
            _asst_msg(),
            _tool_msg("tc-1", "X" * 30000),
            _asst_msg(),
            _tool_msg("tc-2", "Y" * 30000),
            _asst_msg(),
            _tool_msg("tc-3", "Z" * 5000),
        ]
        out = prune_between_fsm_states(messages, current_state="observe")
        old_total = sum(len(str(m.get("content", ""))) for m in messages)
        new_total = sum(len(str(m.get("content", ""))) for m in out)
        # Should reclaim at least 50k chars
        assert old_total - new_total > 50_000, (
            f"pruning should reclaim >50k chars; old={old_total} new={new_total}"
        )

    def test_preserves_system_and_recent_user_messages(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "show data"},
            _asst_msg(),
            _tool_msg("tc-1", "X" * 20000),
            _asst_msg(),
            _tool_msg("tc-2", "Y" * 15000),
            _asst_msg(),
            _tool_msg("tc-3", "Z" * 5000),
        ]
        out = prune_between_fsm_states(messages, current_state="observe")
        # System prompt must be first
        assert out[0]["role"] == "system"
        assert out[0]["content"] == "You are helpful"
        # User query must be preserved
        user_msgs = [m for m in out if m.get("role") == "user"]
        assert len(user_msgs) >= 1
        assert user_msgs[0]["content"] == "show data"


# ── Edge cases ───────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_empty_messages(self):
        assert prune_between_fsm_states([], current_state="act") == []

    def test_no_tool_results(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        out = prune_between_fsm_states(messages, current_state="act")
        assert out == messages

    def test_only_one_tool_result(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
            _asst_msg(),
            _tool_msg("tc-1", "X" * 20000),
        ]
        out = prune_between_fsm_states(messages, current_state="act")
        # Only one tool result — nothing to prune, keep as-is
        assert len(out) == len(messages)

    def test_checkpoint_is_single_message(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "show data"},
            _asst_msg(),
            _tool_msg("tc-1", "X" * 25000),
            _asst_msg(),
            _tool_msg("tc-2", "Y" * 25000),
            _asst_msg(),
            _tool_msg("tc-3", "Z" * 5000),
        ]
        out = prune_between_fsm_states(messages, current_state="observe")
        checkpoints = [
            m for m in out
            if m.get("role") == "assistant" and "checkpoint" in str(m.get("content", "")).lower()
        ]
        assert len(checkpoints) == 1, "should have exactly one checkpoint"
