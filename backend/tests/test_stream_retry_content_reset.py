"""content_reset contract — upstream root-cause fix for duplicate answers.

Systemic fix philosophy (user mandate, 2026-10-20): duplicate answers must be
prevented where they are BORN, not caught afterwards by the dedupe guard.

The duplicated-answer family had these birth sites, now each fixed at source:

1. Frontend done-handler "longer wins" merge let a client-side double append
   overwrite the server row (fixed: server-authoritative done, Chat.jsx).
2. Refinement flow client/server divergence: server discarded the streamed
   answer via a nudge, client kept it; the done-merge then re-added it
   (same fix as #1).
3. ``_stream_llm_with_tools`` mid-stream retry: the generator reset only its
   OWN buffers before re-streaming, so any consumer that had already
   accumulated the failed attempt's output appended the retry's full text on
   top. The generator now yields ``("content_reset", "")`` BEFORE the retry
   and every consumer discards the void attempt (server accumulator cleared,
   client bubble replaced with "").

This file pins contract #3: the generator MUST announce the reset before any
post-retry delta, and the v3 consumer MUST honor it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import httpx
import pytest

from app.routers.agents import _stream_llm_with_tools

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)

_TOOL_CHOICE = {"type": "function", "function": {"name": "any_tool"}}

_RETRY_SSE = (
    'data: {"choices":[{"delta":{"content":"clean retry text"}}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


def _handler(calls: dict):
    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload = json.loads(request.content.decode())
        if payload.get("tool_choice"):
            # First attempt: provider rejects forced tool_choice (the live
            # DeepSeek behaviour that started the duplicate-answer hunt).
            calls["first_had_tool_choice"] = True
            return httpx.Response(400, json={"error": "tool_choice unsupported"})
        calls["second_lacked_tool_choice"] = True
        return httpx.Response(200, text=_RETRY_SSE)
    return handler


def _stream_events():
    calls = {"n": 0, "first_had_tool_choice": False, "second_lacked_tool_choice": False}
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(calls)))
    events = asyncio.run(_drain(client))
    return events, calls


async def _drain(client):
    out = []
    async for ev_type, ev_data in _stream_llm_with_tools(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "any_tool"}}],
        tool_choice=_TOOL_CHOICE,
        client=client,
    ):
        out.append((ev_type, ev_data))
    return out


class TestGeneratorAnnouncesReset:
    def test_reset_emitted_before_retry_deltas(self):
        events, calls = _stream_events()
        assert calls["first_had_tool_choice"], "first attempt must carry tool_choice"
        assert calls["second_lacked_tool_choice"], "retry must drop tool_choice"
        types = [t for t, _ in events]
        assert "content_reset" in types, (
            "generator must announce the reset so consumers discard the void "
            "attempt before the retry re-streams"
        )
        reset_idx = types.index("content_reset")
        delta_after = [i for i, t in enumerate(types) if t == "delta"]
        assert delta_after and all(i > reset_idx for i in delta_after), (
            "every post-retry delta must come AFTER the reset event"
        )

    def test_retry_still_delivers_clean_content(self):
        events, _ = _stream_events()
        done = [d for t, d in events if t == "done"]
        assert done and "clean retry text" in done[0]

    def test_no_reset_when_first_attempt_succeeds(self):
        """Without a failure there is no reset — normal streaming is untouched."""

        async def run():
            ok_handler_calls = {"n": 0}

            async def ok_handler(request: httpx.Request) -> httpx.Response:
                ok_handler_calls["n"] += 1
                return httpx.Response(200, text=_RETRY_SSE)

            client = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))
            out = []
            async for ev in _stream_llm_with_tools(
                [{"role": "user", "content": "q"}], tools=None,
                tool_choice=None, client=client,
            ):
                out.append(ev)
            return out

        events = asyncio.run(run())
        assert all(t != "content_reset" for t, _ in events)


class TestConsumerHonorsReset:
    """The v3 loop must CLEAR its accumulator and the client bubble on reset,
    or the retry's text glues onto the partial first attempt."""

    def _source(self) -> str:
        with open(_AGENTS_PATH) as f:
            return f.read()

    def test_v3_streaming_consumer_handles_content_reset(self):
        source = self._source()
        # Inside add_message_stream's streaming consumption: reset clears the
        # server-side accumulator and emits content_replace("") to the client.
        assert source.count('ev_type == "content_reset"') >= 1, (
            "v3 streaming consumer must handle content_reset"
        )
        assert 'assistant_content = ""' in source

    def test_reset_clears_client_bubble_not_appends(self):
        """The SSE emitted on reset is a content_replace with EMPTY content —
        the client contract replaces the bubble, so retry deltas rebuild from
        a clean slate instead of gluing onto the void attempt."""
        source = self._source()
        pattern = re.compile(
            r'content_reset.*?content_replace", "content": ""', re.DOTALL
        )
        assert pattern.search(source), (
            "on reset the consumer must emit content_replace with empty content"
        )
