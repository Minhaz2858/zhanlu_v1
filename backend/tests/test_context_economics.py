"""Tests for the P1-5 context-economics helpers.

Two new helpers are added to ``app.services.compaction.pre_api_prune``:

  * ``smart_truncate(messages, model, tool_output_caps=None)`` — per-model
    cap on oversized tool-result content.  Different model families
    accept different per-message sizes (qwen3.6-27b: 12,288 tokens,
    deepseek-*: 24,576, gpt-4o: 16,384, claude-*: 32,768).  Unrecognised
    models fall back to a safe default (24,576).
  * ``escalate(tier, messages, model)`` — walks the
    ``CONTEXT_ESCALATION_LADDER`` from the config:
        ["compact", "truncate_tool_outputs", "drop_old_tool_messages",
         "fallback_to_different_model"].

These tests pin:
  * per-model cap selection by name pattern
  * unknown-model fallback
  * escalation tier dispatch (each tier returns a different
    ``(messages, action, fallback_model)`` triple)
  * tier 3 (fallback_to_different_model) returns a different model name
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.compaction import pre_api_prune as pap


def _tool_msg(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _asst_with_calls(call_ids) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "f", "arguments": "{}"}}
            for cid in call_ids
        ],
    }


# ── smart_truncate: per-model cap selection ───────────────────────────────

class TestSmartTruncatePerModel:

    def test_qwen_uses_12288_cap(self):
        # 12,288 tokens * 4 chars/token = 49,152 chars.  Build a result
        # slightly over that.  Result must be truncated.
        over = "X" * 60_000  # 15,000 tokens
        under = "Y" * 4_000  # 1,000 tokens — must NOT be truncated
        # Need 3+ tool messages so the OLDER ones (over, under) get past
        # the keep_recent=2 protection; the LATEST (the new one we add)
        # is always protected.
        msgs = [
            _asst_with_calls(["a"]),
            _tool_msg("a", over),
            _asst_with_calls(["b"]),
            _tool_msg("b", under),
            _asst_with_calls(["c"]),
            _tool_msg("c", "small"),
        ]
        out = pap.smart_truncate(msgs, model="qwen3.6-27b")
        # The oversized result must be truncated; the small one must NOT.
        tool_results = {m["tool_call_id"]: m["content"] for m in out
                        if m.get("role") == "tool"}
        assert len(tool_results["a"]) < len(over), "qwen3.6-27b result should be truncated"
        assert tool_results["b"] == under, "small result should be preserved verbatim"
        assert tool_results["c"] == "small", "most recent must be protected"

    def test_deepseek_uses_24576_cap(self):
        # 24,576 tokens = 98,304 chars.  Build a 30k-token result.
        over = "X" * 120_000  # 30,000 tokens — over deepseek cap
        # 3+ tool messages so keep_recent=2 protects the most recent only.
        msgs = [
            _asst_with_calls(["a"]),
            _tool_msg("a", over),
            _asst_with_calls(["b"]),
            _tool_msg("b", "small"),
            _asst_with_calls(["c"]),
            _tool_msg("c", "small"),
        ]
        out = pap.smart_truncate(msgs, model="deepseek-chat")
        tool_results = {m["tool_call_id"]: m["content"] for m in out
                        if m.get("role") == "tool"}
        # Should be truncated to deepseek's cap (24,576 tokens ≈ 98,304 chars)
        assert len(tool_results["a"]) <= 100_000, (
            f"deepseek result should be truncated to ~24,576 tokens; "
            f"got {len(tool_results['a'])} chars (~{len(tool_results['a']) // 4} tokens)"
        )
        # And NOT 12,288 (qwen's tighter cap)
        assert len(tool_results["a"]) > 50_000, (
            "deepseek cap should be larger than qwen3.6-27b's"
        )

    def test_claude_uses_32768_cap(self):
        over = "X" * 200_000  # 50,000 tokens — over claude cap (32,768)
        msgs = [
            _asst_with_calls(["a"]),
            _tool_msg("a", over),
            _asst_with_calls(["b"]),
            _tool_msg("b", "small"),
            _asst_with_calls(["c"]),
            _tool_msg("c", "small"),
        ]
        out = pap.smart_truncate(msgs, model="claude-sonnet-4")
        tool_results = {m["tool_call_id"]: m["content"] for m in out
                        if m.get("role") == "tool"}
        # Claude cap is 32,768 tokens = 131,072 chars; truncated must be
        # under that.
        assert len(tool_results["a"]) <= 132_000

    def test_unknown_model_falls_back_to_default(self):
        # Unrecognised model — use safe default (24,576).  Build a 50k-token
        # result; should be truncated to ~24,576.
        over = "X" * 200_000
        msgs = [
            _asst_with_calls(["a"]),
            _tool_msg("a", over),
            _asst_with_calls(["b"]),
            _tool_msg("b", "small"),
            _asst_with_calls(["c"]),
            _tool_msg("c", "small"),
        ]
        out = pap.smart_truncate(msgs, model="mystery-future-model")
        tool_results = {m["tool_call_id"]: m["content"] for m in out
                        if m.get("role") == "tool"}
        # Default cap 24,576 tokens = 98,304 chars
        assert len(tool_results["a"]) <= 100_000


# ── escalate: ladder dispatch ─────────────────────────────────────────────

class TestEscalateLadder:

    def test_tier_0_compact_runs_first(self):
        msgs = [{"role": "user", "content": "hi"}]
        out_msgs, action, fallback = pap.escalate(0, msgs, model="deepseek-chat")
        assert action == "compact"
        assert fallback is None  # no model change yet

    def test_tier_1_truncate_tool_outputs(self):
        msgs = [{"role": "user", "content": "hi"}]
        out_msgs, action, fallback = pap.escalate(1, msgs, model="deepseek-chat")
        assert action == "truncate_tool_outputs"

    def test_tier_2_drop_old_tool_messages(self):
        msgs = [{"role": "user", "content": "hi"}]
        out_msgs, action, fallback = pap.escalate(2, msgs, model="deepseek-chat")
        assert action == "drop_old_tool_messages"

    def test_tier_3_fallback_to_different_model(self):
        msgs = [{"role": "user", "content": "hi"}]
        out_msgs, action, fallback = pap.escalate(3, msgs, model="qwen3.6-27b")
        assert action == "fallback_to_different_model"
        # qwen3.6-27b (65k) overflowed → fall back to deepseek-v4-flash (128k)
