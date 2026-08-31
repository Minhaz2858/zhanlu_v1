"""TDD failing tests for true token streaming in the v3 agentic loop.

These tests pin down the desired behaviour BEFORE the implementation
exists. They follow the same AST-based structural pattern used by
``test_v3_streaming_no_dsml_leak.py`` so they run without a live LLM
or DB stack.

Target behaviour this file enforces:

1. A new async generator ``_stream_llm_with_tools`` exists in agents.py
   and yields the documented event tuples.
2. ``_stream_llm_with_tools`` is invoked by the v3 ``add_message_stream``
   loop in place of the buffered ``_call_llm_with_tools`` call.
3. A module-level kill-switch ``STREAM_TOKEN_DELTAS`` exists so the
   feature can be disabled instantly without code changes.
4. The old, broken ``_stream_llm_final_response`` helper is either
   removed OR its tool_call-merge logic is repaired (the current
   implementation at line ~3044 overwrites ``raw_tool_calls`` with each
   chunk instead of accumulating fragments — broken for parallel tool
   calls).
5. The single "big delta" emit at the end of the v3 loop becomes
   conditional: it must NOT re-emit content that was already streamed
   incrementally.

Pure-Python unit tests below (no AST) cover the tool-call fragment
reassembly algorithm — the core of streaming-with-tools. They import
the function once it exists.
"""
import ast
import os
import json

import pytest

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_source() -> str:
    with open(_AGENTS_PATH) as f:
        return f.read()


def _find_function(source: str, name: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return source, node
    return source, None


def _find_name_in_module(source: str, name: str):
    """Return True if ``name`` is assigned/defined at module level."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return True
    return False


# ---------------------------------------------------------------------------
# Structural tests — RED first
# ---------------------------------------------------------------------------

def test_stream_llm_with_tools_generator_exists():
    """The new streaming-with-tools generator must exist.

    It is the single source of truth for streamed LLM calls inside the
    agentic loop: emits ``("delta", token)`` per token, accumulates
    ``tool_calls`` fragments, and yields a single terminal event
    (``done`` / ``tool_calls`` / ``error``).
    """
    source = _load_source()
    _, func = _find_function(source, "_stream_llm_with_tools")
    assert func is not None, (
        "_stream_llm_with_tools generator must be defined in agents.py "
        "to stream tokens live while reassembling fragmented tool_calls."
    )
    assert isinstance(func, ast.AsyncFunctionDef), (
        "_stream_llm_with_tools must be `async def` (it is an async generator)."
    )


def test_stream_token_deltas_killswitch_exists():
    """A module-level kill-switch ``STREAM_TOKEN_DELTAS`` must exist so
    the feature can be reverted instantly if a provider misbehaves."""
    source = _load_source()
    assert _find_name_in_module(source, "STREAM_TOKEN_DELTAS"), (
        "STREAM_TOKEN_DELTAS flag must be defined at module level in "
        "agents.py — instant rollback if streaming causes provider issues."
    )


def test_v3_loop_invokes_stream_generator():
    """The v3 ``add_message_stream`` loop must drive the streaming
    generator with ``async for`` instead of awaiting the buffered
    ``_call_llm_with_tools``.

    Acceptable forms:
      * `async for ... in _stream_llm_with_tools(...)`
      * or the buffered call still present but guarded by
        ``if not STREAM_TOKEN_DELTAS:`` fallback branch.
    """
    source = _load_source()
    _, func = _find_function(source, "add_message_stream")
    assert func is not None, "add_message_stream endpoint must exist"

    found_stream = False
    for node in ast.walk(func):
        if not isinstance(node, ast.AsyncFor):
            continue
        if not isinstance(node.iter, ast.Call):
            continue
        callee = node.iter.func
        name = None
        if isinstance(callee, ast.Name):
            name = callee.id
        elif isinstance(callee, ast.Attribute):
            name = callee.attr
        if name == "_stream_llm_with_tools":
            found_stream = True
            break

    assert found_stream, (
        "add_message_stream must contain an `async for ... in "
        "_stream_llm_with_tools(...)` loop that streams tokens live. "
        "Currently the loop awaits the buffered _call_llm_with_tools "
        "and emits one big delta at the end (no typing effect)."
    )


def test_broken_stream_llm_final_response_is_removed_or_fixed():
    """The legacy ``_stream_llm_final_response`` helper is broken: line
    ~3044 does ``raw_tool_calls = tc`` (overwrite) instead of merging
    fragments by index — corrupted when a tool call spans multiple
    chunks or when parallel tool calls interleave.

    Either remove it (preferred — DRY) or repair the merge logic.
    We accept either: function gone, OR function present but its body
    uses a dict keyed by ``index`` to accumulate fragments.
    """
    source = _load_source()
    _, func = _find_function(source, "_stream_llm_final_response")
    if func is None:
        return  # removed — preferred

    # Function still present: inspect its body for the overwrite bug.
    # We look for any assignment like `raw_tool_calls = tc` (the buggy
    # line) where the RHS is a bare Name. If present AND no dict-based
    # accumulation exists alongside, fail.
    has_overwrite = False
    has_dict_accum = False
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "raw_tool_calls":
                    if isinstance(node.value, ast.Name):
                        has_overwrite = True
        # Dict-based accumulation shows up as Subscript assignment or
        # a Call to dict.setdefault / setdefault on a dict.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "setdefault":
                has_dict_accum = True

    if has_overwrite and not has_dict_accum:
        pytest.fail(
            "_stream_llm_final_response still has the broken overwrite "
            "(`raw_tool_calls = tc`). Either remove this helper (preferred) "
            "or fix the merge to accumulate fragments by index."
        )


def test_v3_final_delta_emit_is_conditional():
    """The line ``yield ... {"type": "delta", "content": assistant_content}``
    at the end of the v3 loop must NOT fire when the content was already
    streamed incrementally — otherwise the UI shows the text twice.

    We accept either:
      * The big-delta yield is guarded by a condition (e.g.
        ``if not content_streamed:`` or ``if not STREAM_TOKEN_DELTAS:``).
      * The yield is removed entirely (preferred — streamed text is
        already in the UI; the ``done`` event carries the final string).

    Implementation note: this test walks the AST of ``add_message_stream``
    only — it does NOT scan the raw source string (which would
    false-positive on the function's own name ``STREAM_TOKEN_DELTAS``
    appearing in error messages).
    """
    source = _load_source()
    _, func = _find_function(source, "add_message_stream")
    assert func is not None

    # Collect names referenced anywhere inside the function body — used
    # to detect the guard variable without scanning the whole source.
    referenced_names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)

    def _is_delta_yield_of(node, var_name):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Yield):
            return False
        yv = node.value.value
        # The yield value is either a bare Call to json.dumps(...) OR an
        # f-string (JoinedStr) that contains a FormattedValue whose
        # .value is the json.dumps(...) call. Handle both.
        candidates = []
        if isinstance(yv, ast.Call):
            candidates.append(yv)
        elif isinstance(yv, ast.JoinedStr):
            for fv in yv.values:
                if isinstance(fv, ast.FormattedValue) and isinstance(fv.value, ast.Call):
                    candidates.append(fv.value)
        for ycall in candidates:
            if not isinstance(ycall.func, ast.Attribute):
                continue
            if ycall.func.attr != "dumps":
                continue
            if not ycall.args:
                continue
            d = ycall.args[0]
            if not isinstance(d, ast.Dict):
                continue
            is_delta = False
            refs_var = False
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant) and k.value == "type":
                    if isinstance(v, ast.Constant) and v.value == "delta":
                        is_delta = True
                if isinstance(k, ast.Constant) and k.value == "content":
                    if isinstance(v, ast.Name) and v.id == var_name:
                        refs_var = True
            if is_delta and refs_var:
                return True
        return False

    has_big_delta = any(
        _is_delta_yield_of(node, "assistant_content")
        for node in ast.walk(func)
    )

    if not has_big_delta:
        return  # no problematic emit at all — fine

    has_guard = (
        "content_streamed" in referenced_names
        or "STREAM_TOKEN_DELTAS" in referenced_names
    )
    assert has_guard, (
        "v3 loop emits the full assistant_content as one delta. To avoid "
        "duplicating already-streamed text, this emit must be guarded by "
        "either a `content_streamed` flag or the `STREAM_TOKEN_DELTAS` "
        "kill-switch."
    )


# ---------------------------------------------------------------------------
# Algorithm unit tests — these will import the real function once it
# exists. Until then they fail at import time (RED).
# ---------------------------------------------------------------------------

def _import_stream_generator():
    """Lazy import so structural tests above don't fail if the helper
    hasn't been written yet."""
    import sys
    agents_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app", "routers")
    )
    if agents_path not in sys.path:
        sys.path.insert(0, agents_path)
    # Force a fresh import to pick up latest source changes.
    if "agents" in sys.modules:
        del sys.modules["agents"]
    import agents  # noqa: E402
    return agents


def _make_sse_lines(*chunks):
    """Convert a list of dict chunks into OpenAI-style SSE text lines."""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": c}]}))
    lines.append("data: [DONE]")
    return lines


class _FakeAiterLines:
    """Async iterable yielding canned SSE text lines."""
    def __init__(self, lines):
        self._lines = list(lines)

    async def __aiter__(self):
        for line in self._lines:
            yield line
        return


class _FakeStreamResponse:
    """Stand-in for the httpx streaming response object.

    When raise_for_status fires on a real httpx streaming
    response, the body is still unread (_content is unset) —
    accessing .text / .content raises
    httpx.ResponseNotRead. The generator must therefore call
    await response.aread() (or otherwise consume the body)
    before stringifying the error. The fake mirrors that state by
    populating _content only inside aread().
    """
    def __init__(self, lines, status_code=200, error_body=b""):
        self._lines = lines
        self.status_code = status_code
        self._error_body = error_body
        self._content = None  # set by aread()

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            import httpx._content as hc
            req = httpx.Request("POST", "http://fake/llm")
            # Use stream= (NOT content=) so _content is unset —
            # mirrors real httpx behavior when client.stream(...)
            # encounters an error.
            body = self._error_body or b"(empty error body)"
            resp = httpx.Response(
                self.status_code, request=req,
                stream=hc.ByteStream(body),
            )
            raise httpx.HTTPStatusError(
                f"simulated {self.status_code}", request=req, response=resp,
            )

    async def aread(self):
        self._content = self._error_body or b"(empty error body)"
        return self._content

    @property
    def content(self):
        if self._content is None:
            import httpx
            raise httpx.ResponseNotRead()
        return self._content

    def aiter_lines(self):
        return _FakeAiterLines(self._lines)


class _FakeStreamContext:
    """Stand-in for `client.stream(...)` async context manager."""
    def __init__(self, lines, status_code=200, error_body=b''):
        self._resp = _FakeStreamResponse(lines, status_code, error_body)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient used by the generator."""
    def __init__(self, lines, status_code=200, timeout=None,
                 error_body=b''):
        self._lines = lines
        self._status = status_code
        # Body returned by the fake response when raise_for_status
        # fires. Empty by default so the existing happy-path tests
        # are unaffected; the new 5xx regression tests pass a real
        # body so they can assert the body surfaces in the error
        # message.
        self._error_body = error_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        return _FakeStreamContext(self._lines, self._status,
                                  self._error_body)


class _RejectToolsThenSuccessClient:
    """Fake client that rejects tool payloads like local text-only models."""
    def __init__(self, success_lines):
        self._success_lines = list(success_lines)
        self.payloads = []

    def stream(self, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        self.payloads.append(payload)
        if payload.get("tools"):
            return _FakeStreamContext(
                [], 400, b"tools/function calling is not supported"
            )
        return _FakeStreamContext(self._success_lines, 200, b"")


class _RejectToolChoiceThenSuccessClient:
    """Fake client that rejects tool_choice-bearing payloads (DeepSeek
    thinking-mode quirk), succeeding once tool_choice is dropped."""
    def __init__(self, success_lines):
        self._success_lines = list(success_lines)
        self.payloads = []

    def stream(self, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        self.payloads.append(payload)
        if payload.get("tool_choice"):
            return _FakeStreamContext([], 400, b"tool_choice is not supported")
        return _FakeStreamContext(self._success_lines, 200, b"")


@pytest.mark.asyncio
async def test_stream_emits_delta_then_done_for_text_only_response():
    """Plain text response: generator must yield ('delta', token) per
    chunk, then a single terminal ('done', full_text)."""
    agents = _import_stream_generator()
    assert hasattr(agents, "_stream_llm_with_tools"), (
        "_stream_llm_with_tools must exist for algorithm tests."
    )

    canned = _make_sse_lines(
        {"content": "Hello"},
        {"content": ", "},
        {"content": "world!"},
    )

    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "hi"}], tools=None,
        client=_FakeAsyncClient(canned),
    ):
        events.append(ev)

    deltas = [e for e in events if e[0] == "delta"]
    dones = [e for e in events if e[0] == "done"]
    errors = [e for e in events if e[0] == "error"]

    assert not errors, f"unexpected errors: {errors}"
    assert len(deltas) == 3, f"expected 3 deltas, got {deltas}"
    assert "".join(d for _, d in deltas) == "Hello, world!"
    assert len(dones) == 1
    assert dones[0][1] == "Hello, world!", (
        f"terminal done must carry the full accumulated text; got {dones[0]}"
    )


@pytest.mark.asyncio
async def test_stream_reassembles_fragmented_tool_calls():
    """Tool-call deltas arrive fragmented: first chunk carries id+name,
    subsequent chunks append to arguments. Generator must assemble them
    in index order and emit ('tool_calls', assembled) as the terminal
    event — NOT ('done', ...).
    """
    agents = _import_stream_generator()

    canned = _make_sse_lines(
        # First chunk for tool index 0: id + name.
        {"tool_calls": [{
            "index": 0, "id": "call_abc",
            "type": "function",
            "function": {"name": "create_docx", "arguments": ""},
        }]},
        # Subsequent chunks: arguments fragments.
        {"tool_calls": [{
            "index": 0,
            "function": {"arguments": "{\"title\":"},
        }]},
        {"tool_calls": [{
            "index": 0,
            "function": {"arguments": " \"Report\"}"},
        }]},
    )

    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "make a doc"}],
        tools=[{"type": "function", "function": {"name": "create_docx"}}],
        client=_FakeAsyncClient(canned),
    ):
        events.append(ev)

    deltas = [e for e in events if e[0] == "delta"]
    tcs_events = [e for e in events if e[0] == "tool_calls"]
    dones = [e for e in events if e[0] == "done"]

    assert len(tcs_events) == 1, (
        f"expected exactly one terminal tool_calls event, got {tcs_events}"
    )
    assert not dones, "no 'done' when tool_calls were returned"
    assembled = tcs_events[0][1]
    assert isinstance(assembled, list) and len(assembled) == 1
    call = assembled[0]
    assert call["id"] == "call_abc"
    assert call["function"]["name"] == "create_docx"
    assert json.loads(call["function"]["arguments"]) == {"title": "Report"}


@pytest.mark.asyncio
async def test_stream_reassembles_parallel_tool_calls_in_index_order():
    """When the model emits multiple parallel tool calls interleaved by
    index, the generator must assemble them in index order (0, 1, 2...).
    """
    agents = _import_stream_generator()

    canned = _make_sse_lines(
        # Tool 0 first fragment.
        {"tool_calls": [{"index": 0, "id": "call_0",
                         "type": "function",
                         "function": {"name": "search", "arguments": ""}}]},
        # Tool 1 first fragment (interleaved).
        {"tool_calls": [{"index": 1, "id": "call_1",
                         "type": "function",
                         "function": {"name": "fetch", "arguments": ""}}]},
        # Tool 0 arguments.
        {"tool_calls": [{"index": 0,
                         "function": {"arguments": "{\"q\":\"a\"}"}}]},
        # Tool 1 arguments.
        {"tool_calls": [{"index": 1,
                         "function": {"arguments": "{\"id\":1}"}}]},
    )

    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "x"}], tools=None,
        client=_FakeAsyncClient(canned),
    ):
        events.append(ev)

    tcs = [e for e in events if e[0] == "tool_calls"]
    assert len(tcs) == 1
    assembled = tcs[0][1]
    assert len(assembled) == 2
    assert assembled[0]["id"] == "call_0"
    assert assembled[1]["id"] == "call_1"
    assert json.loads(assembled[0]["function"]["arguments"]) == {"q": "a"}
    assert json.loads(assembled[1]["function"]["arguments"]) == {"id": 1}


@pytest.mark.asyncio
async def test_stream_emits_error_terminal_on_http_failure():
    """If the provider returns a 5xx, the generator must emit
    ('error', message) as the terminal event — no done, no tool_calls.

    The error message MUST contain the provider body text (not the
    opaque ``ResponseNotRead`` exception). The provider returns the
    body via a streaming response whose ``_content`` is unset until
    ``.aread()`` runs, so the generator has to consume the body
    explicitly. If it accesses ``e.response.text`` directly, the
    body access raises ``ResponseNotRead`` and that opaque error
    bubbles all the way to the chat — see
    ``test_stream_does_not_surface_response_not_read_on_5xx``.
    """
    agents = _import_stream_generator()

    canned = _make_sse_lines()  # body won't be read — status fails first

    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "hi"}], tools=None,
        client=_FakeAsyncClient(canned, status_code=500,
                                error_body=b"rate limit exceeded"),
    ):
        events.append(ev)

    errors = [e for e in events if e[0] == "error"]
    dones = [e for e in events if e[0] == "done"]
    assert len(errors) == 1, f"expected one error terminal, got {events}"
    assert not dones
    msg = errors[0][1]
    assert "ResponseNotRead" not in msg, (
        f"error message leaked the streaming-not-read exception: {msg!r}"
    )
    assert "rate limit exceeded" in msg, (
        f"expected the provider body to appear in the error message, got {msg!r}"
    )


@pytest.mark.asyncio
async def test_stream_does_not_surface_response_not_read_on_5xx():
    """Regression: when the LLM provider returns a 5xx and the
    streaming body has not yet been read, accessing
    ``e.response.text`` / ``.content`` raises
    ``httpx.ResponseNotRead`` ("Attempted to access streaming
    response content, without having called read()"). Before the
    fix, the chat surfaced that opaque exception string instead of
    the real error body. This test pins the fix: the generator
    must ``await e.response.aread()`` (or otherwise consume the
    body) before stringifying the error.
    """
    agents = _import_stream_generator()

    canned = _make_sse_lines()  # body is unread when status fails
    client = _FakeAsyncClient(canned, status_code=503,
                              error_body=b"service unavailable")
    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "hi"}], tools=None,
        client=client,
    ):
        events.append(ev)

    errors = [e for e in events if e[0] == "error"]
    assert len(errors) == 1, f"expected one error terminal, got {events}"
    msg = errors[0][1]

    # The whole point of this test: the user-visible error must NOT
    # be the streaming-not-read exception string the user was seeing
    # before the fix.
    assert "Attempted to access streaming response content" not in msg, (
        f"Regression: the opaque ResponseNotRead string leaked into "
        f"the user-visible error message: {msg!r}"
    )
    assert "ResponseNotRead" not in msg
    # And the real provider body must come through.
    assert "service unavailable" in msg, f"got {msg!r}"


@pytest.mark.asyncio
async def test_stream_retries_without_tool_choice_when_provider_rejects_it():
    """DeepSeek thinking-mode quirk: a 400 on a tool_choice-bearing payload
    must retry WITHOUT tool_choice. Tools STAY — the platform requires tool
    calling for the agent to function; only the tool_choice field is the
    rejected contract. Pins the deliberate retry contract (2026-08-27)."""
    agents = _import_stream_generator()

    canned = _make_sse_lines({"content": "local answer"})
    client = _RejectToolChoiceThenSuccessClient(canned)
    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "search"}}],
        tool_choice={"type": "function", "function": {"name": "search"}},
        client=client,
    ):
        events.append(ev)

    # First payload carries tool_choice + tools; the retry drops tool_choice
    # but KEEPS tools.
    assert [p.get("tool_choice") for p in client.payloads] == [
        {"type": "function", "function": {"name": "search"}},
        None,
    ]
    assert all(p.get("tools") for p in client.payloads)
    assert ("delta", "local answer") in events
    assert ("done", "local answer") in events
    assert not [e for e in events if e[0] == "error"]


@pytest.mark.asyncio
async def test_stream_surfaces_error_when_provider_rejects_tools_without_tool_choice():
    """A provider that rejects the tools field itself (text-only local
    server) has no fallback — the platform requires tool calling — so the
    error is surfaced as an ('error', ...) event instead of a hang or a
    silent text-only answer. No retry happens when tool_choice is absent."""
    agents = _import_stream_generator()

    canned = _make_sse_lines({"content": "local answer"})
    client = _RejectToolsThenSuccessClient(canned)
    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "search"}}],
        client=client,
    ):
        events.append(ev)

    # Exactly one attempt: no retry (tool_choice was None → immediate raise).
    assert [p.get("tools") for p in client.payloads] == [
        [{"type": "function", "function": {"name": "search"}}],
    ]
    assert any(e[0] == "error" for e in events)
    assert not any(e[0] == "done" for e in events)


@pytest.mark.asyncio
async def test_stream_forwards_reasoning_chunks():
    """If the provider streams ``reasoning_content`` (DeepSeek-R1),
    the generator must surface ('reasoning', text) events so the v3
    loop can keep the existing reasoning_done behaviour working."""
    agents = _import_stream_generator()

    canned = _make_sse_lines(
        {"reasoning_content": "thinking... "},
        {"reasoning_content": "more thinking"},
        {"content": "answer"},
    )

    events = []
    async for ev in agents._stream_llm_with_tools(
        messages=[{"role": "user", "content": "hi"}], tools=None,
        client=_FakeAsyncClient(canned),
    ):
        events.append(ev)

    reasoning = [e for e in events if e[0] == "reasoning"]
    assert len(reasoning) == 2, f"expected 2 reasoning events, got {reasoning}"
    assert "".join(t for _, t in reasoning) == "thinking... more thinking"


# ---------------------------------------------------------------------------
# DI surface check
# ---------------------------------------------------------------------------

def test_stream_generator_accepts_injectable_client():
    """Production code that talks to the network must be testable.
    The generator must accept an optional ``client`` parameter (or
    read a module-level hook) so tests can inject a fake httpx client
    without monkey-patching global state.
    """
    source = _load_source()
    _, func = _find_function(source, "_stream_llm_with_tools")
    assert func is not None, (
        "_stream_llm_with_tools must exist before its DI surface is checked."
    )
    arg_names = [a.arg for a in func.args.args]
    kwonly_names = [a.arg for a in func.args.kwonlyargs]
    has_client_param = "client" in arg_names or "client" in kwonly_names
    has_module_hook = (
        "_LLM_STREAM_CLIENT_FACTORY" in source
        or "LLM_STREAM_CLIENT_FACTORY" in source
    )
    assert has_client_param or has_module_hook, (
        "_stream_llm_with_tools must accept an injectable httpx client "
        "(param `client=` or module-level factory hook) so the streaming "
        "logic is unit-testable without patching globals. "
        f"Found args: {arg_names}, kwonly: {kwonly_names}"
    )
