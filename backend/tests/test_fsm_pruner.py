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

class TestOrphanInvariant:
    """Regression (2026-08-29): dropping old tool messages must strip the
    orphaned tool_calls from the prior assistant messages, otherwise
    DeepSeek/OpenAI reject the request with:
      \"An assistant message with 'tool_calls' must be followed by tool
       messages responding to each 'tool_call_id' (insufficient tool
       messages following tool_calls message)\"
    Triggered once a conversation accumulated >40k chars of tool results
    across multiple turns (conv 945c7cf2) — every subsequent turn 400'd
    before the agent loop could run."""

    def _real_shape(self) -> list[dict]:
        # Two completed turns, each an assistant batch of 3 distinct calls
        # followed by their tool responses (the persisted/rebuild shape).
        def asst(tc_ids, content):
            return {
                "role": "assistant", "content": content,
                "tool_calls": [
                    {"id": tid, "type": "function",
                     "function": {"name": "ask_data_agent", "arguments": "{}"}}
                    for tid in tc_ids
                ],
            }

        return [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "weekly report"},
            asst(["c1a", "c1b", "c1c"], "Weekly report done."),
            {"role": "tool", "tool_call_id": "c1a", "content": "X" * 12000},
            {"role": "tool", "tool_call_id": "c1b", "content": "Y" * 9000},
            {"role": "tool", "tool_call_id": "c1c", "content": "Z" * 3000},
            {"role": "user", "content": "supply chain snapshot"},
            asst(["c2a", "c2b", "c2c"], "Supply chain done."),
            {"role": "tool", "tool_call_id": "c2a", "content": "A" * 2000},
            {"role": "tool", "tool_call_id": "c2b", "content": "B" * 59000},  # huge
            {"role": "tool", "tool_call_id": "c2c", "content": "C" * 1200},
        ]

    def test_no_dangling_tool_calls_after_prune(self):
        messages = self._real_shape()
        out = prune_between_fsm_states(messages, current_state="llm_call")
        assert len(out) < len(messages), "pruning should have kicked in"

        # Every assistant tool_call must have a following tool response.
        emitted_call_ids: set[str] = set()
        tool_ids: set[str] = set()
        for m in out:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    emitted_call_ids.add(tc["id"])
            if m.get("role") == "tool":
                tool_ids.add(m["tool_call_id"])
        dangling = emitted_call_ids - tool_ids
        assert not dangling, f"dangling tool_calls after prune: {dangling}"
        # And every tool message must reference an emitted call (no orphans).
        orphan_tools = tool_ids - emitted_call_ids
        assert not orphan_tools, f"orphaned tool messages after prune: {orphan_tools}"
        # DeepSeek rejects an EMPTY tool_calls array ("Invalid
        # 'messages[N].tool_calls': empty array" 400) — stripped messages
        # must drop the key, not leave [].
        empty_tc = [
            i for i, m in enumerate(out)
            if m.get("role") == "assistant" and m.get("tool_calls") == []
        ]
        assert not empty_tc, f"empty tool_calls array at: {empty_tc}"

    def test_latest_tool_result_preserved_and_checkpoint_present(self):
        messages = self._real_shape()
        out = prune_between_fsm_states(messages, current_state="llm_call")
        latest = next(m for m in out if m.get("tool_call_id") == "c2c")
        assert latest["content"] == "C" * 1200
        assert any(
            m.get("role") == "assistant" and "Checkpoint" in str(m.get("content", ""))
            for m in out
        )

    def _assert_pair_adjacency(self, out):
        """For every assistant(tool_calls) message, the IMMEDIATELY following
        messages must be tool responses covering ALL its call ids — no
        non-tool message may sit between an assistant tool_call and its
        response (DeepSeek 400: 'insufficient tool messages following
        tool_calls')."""
        for i, m in enumerate(out):
            if m.get("role") != "assistant" or not m.get("tool_calls"):
                continue
            need = {tc["id"] for tc in m["tool_calls"]}
            j = i + 1
            got = set()
            while j < len(out) and out[j].get("role") == "tool":
                got.add(out[j].get("tool_call_id"))
                j += 1
            missing = need - got
            assert not missing, (
                f"assistant tool_calls at {i} not IMMEDIATELY followed by "
                f"their tool responses; missing={sorted(missing)}; "
                f"next msg after tools: {out[j] if j < len(out) else 'END'}"
            )

    def test_checkpoint_never_splits_tool_pair(self):
        """Regression (2026-08-29, conv 8e749a1e): ONE assistant batch of 12
        tool_calls with 12 tool responses. The pruner replaced the 11 old
        results with a checkpoint but inserted it BETWEEN the assistant
        message and the KEPT (12th) tool response — every follow-up turn
        400'd with 'insufficient tool messages following tool_calls'.
        The checkpoint must land AFTER the contiguous tool-result run."""
        def asst(tc_ids, content):
            return {
                "role": "assistant", "content": content,
                "tool_calls": [
                    {"id": tid, "type": "function",
                     "function": {"name": "ask_data_agent", "arguments": "{}"}}
                    for tid in tc_ids
                ],
            }

        ids = [f"call_{i}" for i in range(12)]
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "supply chain snapshot"},
            asst(ids, "Your deliverable is attached."),
        ] + [
            {"role": "tool", "tool_call_id": tid, "content": "X" * 12000}
            for tid in ids
        ] + [
            {"role": "user", "content": "sales snapshot"},
        ]
        out = prune_between_fsm_states(messages, current_state="llm_call")
        # Pruning must have fired (11 of 12 results replaced)
        assert len(out) < len(messages)
        self._assert_pair_adjacency(out)

    def test_existing_orphan_shapes_preserve_adjacency(self):
        messages = self._real_shape()
        out = prune_between_fsm_states(messages, current_state="llm_call")
        self._assert_pair_adjacency(out)


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
