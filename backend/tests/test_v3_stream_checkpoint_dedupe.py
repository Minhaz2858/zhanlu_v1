"""Regression: v3 stream checkpoint must replace the previous partial,
not accumulate it.

The bug
-------
The v3 stream checkpoint at ``agents.py:~6490-6520`` writes a partial
assistant message to the DB every time a tool call returns a report
card or artifact. The cleanup at line 6502 was supposed to drop the
*previous* partial before appending the new one, but its condition
required the previous partial to have NO tool_calls. Since partials
carry ``tool_calls_for_frontend`` (the accumulated tool calls), the
condition never fired. Result: each checkpoint appended a new partial
without dropping the old, so ``conv.messages`` grew with duplicate
assistant messages.

User-visible symptom: "I asked one question and it's giving me multiple
answers when I refresh. All of inputted text is gone and multiple
answers are showing from the agent."

The fix
-------
Change the cleanup condition to drop a trailing assistant message
whenever its content is empty — regardless of whether it has
tool_calls. The previous partial (empty content + tool_calls) is
dropped; the new partial takes its place. The final assistant message
(with content) is appended AFTER the loop, not by the checkpoint, so
the cleanup does not accidentally remove it.

Why source-text tests (not a DB-integration test)?
--------------------------------------------------
The buggy code is inline inside ``add_message_stream`` (the v3 SSE
generator). A full integration test would require booting the FastAPI
app, opening a websocket/SSE stream, and running a multi-iteration
LLM loop — heavy and flaky. A source-text test pins the contract:
the cleanup must drop empty-content assistant messages. A refactor
that reintroduces the ``not base[-1].get("tool_calls")`` clause fails
loudly.
"""
from __future__ import annotations

from pathlib import Path

# backend/tests/test_v3_stream_checkpoint_dedupe.py
# → parents[0] = tests/
# → parents[1] = backend/
# → parents[2] = zhanlu_7_30/
AGENTS_PY = (Path(__file__).resolve().parent.parent / "app" / "routers" / "agents.py").read_text(encoding="utf-8")


class TestV3StreamCheckpointDedupe:
    """Pin the v3 stream checkpoint's cleanup condition."""

    def test_checkpoint_cleanup_drops_empty_content_partials(self):
        """The cleanup must drop a trailing assistant message whenever
        its content is empty — the tool_calls check was the bug."""
        # The cleanup line must NOT require ``not base[-1].get("tool_calls")``
        # because partials always have tool_calls set (that's how they
        # carry report cards to the frontend).
        assert 'if base and base[-1].get("role") == "assistant" and not base[-1].get("content") and not base[-1].get("tool_calls")' not in AGENTS_PY, (
            "The cleanup condition at the v3 stream checkpoint still "
            "requires 'no tool_calls'. This is the duplication bug: partials "
            "always carry tool_calls, so the cleanup never fires and each "
            "checkpoint appends a new partial without dropping the previous one."
        )

    def test_checkpoint_cleanup_drops_empty_content_assistant(self):
        """The cleanup MUST drop trailing empty-content assistant messages
        (regardless of tool_calls) so each checkpoint replaces the previous partial."""
        assert 'if base and base[-1].get("role") == "assistant" and not base[-1].get("content")' in AGENTS_PY, (
            "The cleanup must drop a trailing assistant message whenever its "
            "content is empty. Without this, each stream checkpoint appends "
            "a new partial without removing the previous one, and "
            "conv.messages grows with duplicate assistant messages "
            "(visible as 'multiple answers' on refresh)."
        )

    def test_checkpoint_appends_partial_after_cleanup(self):
        """The cleanup must come BEFORE the append, so the previous partial
        is dropped before the new one is added."""
        # The v3 stream checkpoint uses `base.append(...)` (line 6504).
        # The cleanup uses `base = base[:-1]` (line 6503).
        # The cleanup must come before the append.
        cleanup_idx = AGENTS_PY.find('base = base[:-1]')
        append_idx = AGENTS_PY.find('base.append({')
        assert cleanup_idx != -1, "Cleanup (base = base[:-1]) not found"
        assert append_idx != -1, "Append (base.append) not found"
        assert cleanup_idx < append_idx, (
            f"The cleanup (index {cleanup_idx}) must come BEFORE the append "
            f"(index {append_idx}), so the previous partial is dropped "
            "before the new one is added."
        )

    def test_final_append_drops_trailing_partials(self):
        """The final ``messages.append(assistant_msg)`` must drop any
        trailing empty-content assistant messages (the partials the
        checkpoints left behind) before appending the authoritative
        final response. Otherwise the conv keeps one partial per
        iteration PLUS the final, and the user sees duplicate answers."""
        # Find the final assistant_msg append (after the loop) and check
        # that it is preceded by a while-loop that pops empty trailing
        # assistant messages.
        final_append_idx = AGENTS_PY.find(
            'messages.append(assistant_msg)',
            AGENTS_PY.find('async def add_message_stream'),
        )
        assert final_append_idx != -1, "Final append not found in add_message_stream"
        # The dedupe while-loop should appear just before the final append
        # (within ~1500 chars back). Look for the pop loop pattern.
        # Search backwards for "while" + "messages.pop()" nearby.
        snippet = AGENTS_PY[max(0, final_append_idx - 1500):final_append_idx]
        assert 'while' in snippet and 'messages.pop()' in snippet, (
            "The final append must be preceded by a while-loop that pops "
            "trailing empty-content assistant messages (the partials the "
            "stream checkpoints left behind). Otherwise the conv keeps one "
            "partial per iteration plus the final, and the user sees "
            "duplicate answers on refresh."
        )
