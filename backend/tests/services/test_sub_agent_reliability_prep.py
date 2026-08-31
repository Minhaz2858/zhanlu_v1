"""Tests for ``pre_call_prep`` orphan-tool-pairs fix (2026-08-25).

Validates that ``app.services.sub_agent_reliability.pre_call_prep``
strips ``assistant.tool_calls`` whose matching ``tool`` messages have
been lost from the middle of the conversation history — the precise
shape that triggers ``HTTP 400 'insufficient tool messages following
tool_calls message'`` against deepseek/qwen3/vLLM APIs.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))

import pytest

from app.services.sub_agent_reliability import pre_call_prep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _asst_with_tcs(*tcs: str, content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tcid,
                "type": "function",
                "function": {"name": "demo", "arguments": "{}"},
            }
            for tcid in tcs
        ],
    }


def _tool_msg(tool_call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


# ---------------------------------------------------------------------------
# Orphan removal — mid-conversation
# ---------------------------------------------------------------------------
class TestPreCallPrepOrphanToolCalls:
    def test_strip_orphan_assistant_tool_calls(self):
        # The exact shape that surfaced in session a5ceaea2 on 2026-08-25:
        # assistant with 5 tool_calls immediately followed by a user
        # message (no tool messages between). DeepSeek returned 400.
        messages = [
            {"role": "system", "content": "you are a helpful assistant"},
            {"role": "user", "content": "weekly market overview"},
            _asst_with_tcs("call_00_tK", "call_01_IC", "call_00_Eb",
                           "call_01_xi", "call_02_Wd", content="[FORECAST]"),
            # GAP — what was supposed to be 5 tool messages is missing.
            {"role": "user", "content": "hi"},
        ]
        pre_call_prep(messages)

        # All five tool_calls must be stripped (orphan).
        assert not messages[2].get("tool_calls"), \
            f"orphan tool_calls NOT stripped: {messages[2]}"
        assert messages[2]["role"] == "assistant"

    def test_keep_paired_assistant_tool_calls(self):
        # Healthy conversation: assistant-with-tool_calls immediately
        # followed by all matching tool messages. Must survive intact.
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            _asst_with_tcs("aa", "bb"),
            _tool_msg("aa", content="alpha-result"),
            _tool_msg("bb", content="beta-result"),
            {"role": "assistant", "content": "final answer"},
        ]
        pre_call_prep(messages)
        assert len(messages[2]["tool_calls"]) == 2
        assert messages[3]["tool_call_id"] == "aa"
        assert messages[4]["tool_call_id"] == "bb"

    def test_strip_partial_orphan_only(self):
        # Assistant with 3 tool_calls; only the first 2 have matching
        # tool messages. The 3rd is orphan and must be stripped; the
        # first 2 must survive.
        messages = [
            _asst_with_tcs("aa", "bb", "cc"),
            _tool_msg("aa"),
            _tool_msg("bb"),
            {"role": "user", "content": "hi"},  # 'cc' is orphan
        ]
        pre_call_prep(messages)
        kept_ids = [tc["id"] for tc in messages[0]["tool_calls"]]
        assert "aa" in kept_ids
        assert "bb" in kept_ids
        assert "cc" not in kept_ids, f"orphan 'cc' leaked through: {kept_ids}"


# ---------------------------------------------------------------------------
# Idempotence + no-break
# ---------------------------------------------------------------------------
class TestPreCallPrepIdempotent:
    def test_idempotent_on_clean_messages(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ]
        pre_call_prep(messages)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"
