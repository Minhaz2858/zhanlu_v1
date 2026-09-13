"""Tests for the v3 SSE terminal-frame guarantee (_guarantee_done).

The v3 stream commits the assistant message on the ``done`` frame. A stream
that ends WITHOUT any terminal frame (silent early return from a mid-loop
code path) used to leave the frontend stuck in "Running" forever — observed
2026-09-03: a Data Analyst turn streamed its first tool call, saved its
checkpoint, then the generator returned; the UI stayed on "Querying the
bound data source" for 257s+.

``_guarantee_done`` must:
  * pass normal terminal frames through unchanged,
  * emit a synthetic ``error`` frame when the inner generator ends without
    ever emitting a terminal frame,
  * keep the existing fallback-``done`` behaviour for raised exceptions.
"""

import asyncio
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _collect(frames):
    from app.routers.agents import _guarantee_done

    async def _run():
        out = []
        async for frame in _guarantee_done(_agen(frames)):
            out.append(frame)
        return out

    return asyncio.run(_run())


async def _agen(frames):
    for f in frames:
        yield f


def _frame(etype, **extra):
    payload = {"type": etype, **extra}
    return "data: " + json.dumps(payload) + "\n\n"


def _types(out):
    types = []
    for f in out:
        if isinstance(f, str) and '"type":' in f:
            try:
                data = f.split("data: ", 1)[1].strip()
                types.append(json.loads(data).get("type"))
            except Exception:
                continue
    return types


class TestGuaranteeDone:
    def test_normal_done_passthrough_no_synthetic_error(self):
        out = _collect([
            _frame("delta", content="hi"),
            _frame("done", content="hi", trace=[], conversation={}),
        ])
        assert _types(out) == ["delta", "done"], _types(out)

    def test_silent_end_without_terminal_emits_error(self):
        # The regression: the inner generator simply RETURNS after streaming
        # non-terminal frames (tool progress, deltas) — no done/paused/error.
        out = _collect([
            _frame("tool_progress", tool_calls=[]),
            _frame("delta", content="partial"),
        ])
        types = _types(out)
        assert types[-1] == "error", types
        assert "error" in types

    def test_empty_stream_emits_error(self):
        out = _collect([])
        assert _types(out) == ["error"], _types(out)

    def test_paused_is_terminal_no_error(self):
        out = _collect([
            _frame("paused", conversation={}),
        ])
        assert _types(out) == ["paused"], _types(out)
