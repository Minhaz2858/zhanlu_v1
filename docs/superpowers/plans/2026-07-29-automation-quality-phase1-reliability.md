# Automation Quality Phase 1 — Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One transient LLM error no longer kills a run — bounded backoff retry (status-code classified, Retry-After honored) on streaming + non-streaming paths, no more 4xx failover in `call_llm`, and data_sync runs fail fast with an explicit error when a bound source is unreachable.

**Architecture:** A small `services/llm_retry.py` holds the retry primitives (transient check, status/Retry-After extraction, backoff, attempt budget, async retry loop). `agents.py` integrates thin wrappers: non-streaming swaps its log-and-break branch for `call_with_transient_retry`; streaming wraps its consume loop, gated on the existing `content_streamed` flag (retry only before the first client-visible chunk), emits an `llm_retry` SSE event, and honors `should_fallback` (buffered attempt with `model_override`) / `should_compress` (one compaction pass, `continue`) instead of dropping them. The executor's `_is_transient_error` swaps substring matching for the classifier (contract preserved). The data_sync preflight probes each bound source via the canonical `get_connector(kb)` + `SELECT 1` (or HEAD for API sources) and fails through the existing `_mark_failed`/`schedule_retry` machinery.

**Tech Stack:** FastAPI, httpx, SQLAlchemy, existing `api_error_classifier` / `provider_fallback` / `db` connector layer.

**Spec:** `docs/superpowers/specs/2026-07-29-automation-quality-reliability-design.md` (§4). Phase 0 plan/tests already landed.

## Global Constraints

- No new dependencies.
- Test DB recipe: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"`, then prefix pytest with `DATABASE_URL="sqlite:///./test_runtime.db"`.
- **RAM limited**: explicit test file paths only; never full suite; no `-n`.
- Do NOT modify the line `pool.submit(_run_agent_in_conversation, ...)` in `automation_executor.py` — a gate test asserts it as a single-line literal.
- `agents.py` is 300KB+: use targeted `replace_in_file` with the exact snippets below; never rewrite the file.
- Frontend SSE consumers use `else if (evt.type === ...)` chains (Chat.jsx, SkillAgent.jsx, useAgentBuilder.js) — unknown event types are silently ignored, so the new `llm_retry` event is safe with zero frontend changes.
- Existing contract to preserve: `tests/test_automation_runtime_agent.py:306-333` asserts `_is_transient_error` behavior (503/timeout/429 → True; "invalid prompt: empty" / "403 forbidden" → False).

---

### Task 1: `llm_retry` primitives

**Files:**
- Create: `backend/app/services/llm_retry.py`
- Test: `backend/tests/test_llm_retry.py`

**Interfaces:**
- Produces: `TRANSIENT_REASONS`, `is_transient(ce) -> bool`, `extract_status_code(exc) -> Optional[int]`, `retry_after_seconds(exc) -> Optional[float]`, `max_attempts_for(ce) -> int`, `next_backoff(attempt, retry_after=None) -> float`, `call_with_transient_retry(factory, *, on_retry=None, _sleep=asyncio.sleep)`. Consumed by Tasks 2, 3, 4, 5.

- [ ] **Step 1: Write the failing test**

```python
"""llm_retry primitives: classification gate, Retry-After, backoff, loop."""
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import httpx

from app.services.api_error_classifier import FailoverReason, classify_api_error
from app.services import llm_retry as lr


def _http_error(status: int, headers=None) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(status, headers=headers or {}),
    )


def test_is_transient_gate():
    assert lr.is_transient(classify_api_error(_http_error(429)))
    assert lr.is_transient(classify_api_error(_http_error(503)))
    assert lr.is_transient(classify_api_error(_http_error(500)))
    assert not lr.is_transient(classify_api_error(_http_error(400)))
    assert not lr.is_transient(classify_api_error(_http_error(401)))
    assert not lr.is_transient(classify_api_error(_http_error(404)))
    # unknown errors are fail-safe: no retry storm
    assert not lr.is_transient(classify_api_error(ValueError("weird")))


def test_extract_status_code():
    assert lr.extract_status_code(_http_error(429)) == 429
    assert lr.extract_status_code(ValueError("nope")) is None


def test_retry_after_seconds():
    assert lr.retry_after_seconds(_http_error(429, {"Retry-After": "7"})) == 7.0
    assert lr.retry_after_seconds(_http_error(429)) is None
    assert lr.retry_after_seconds(_http_error(429, {"Retry-After": "junk"})) is None


def test_max_attempts_budget():
    assert lr.max_attempts_for(classify_api_error(_http_error(429))) == 3
    assert lr.max_attempts_for(classify_api_error(_http_error(500))) == 2
    assert lr.max_attempts_for(classify_api_error(ValueError("x"))) == 1


def test_next_backoff_honors_retry_after_and_caps(monkeypatch):
    monkeypatch.setattr(lr.random, "uniform", lambda a, b: b)
    assert lr.next_backoff(0, None) == 1.0
    assert lr.next_backoff(1, None) == 2.0
    assert lr.next_backoff(10, None) == 30.0  # capped
    assert lr.next_backoff(0, 5.0) == 6.0     # retry-after + <=1s jitter
    assert lr.next_backoff(0, 100.0) == 30.0  # capped


def test_retry_loop_succeeds_after_transients():
    calls = {"n": 0}
    retries = []

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(503)
        return "ok"

    async def fake_sleep(_):
        pass

    async def on_retry(next_attempt, delay, ce):
        retries.append((next_attempt, ce.reason))

    result = asyncio.run(
        lr.call_with_transient_retry(factory, on_retry=on_retry, _sleep=fake_sleep)
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert retries == [(2, FailoverReason.overloaded), (3, FailoverReason.overloaded)]


def test_retry_loop_raises_permanent_immediately():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _http_error(400)

    async def fake_sleep(_):
        pass

    try:
        asyncio.run(lr.call_with_transient_retry(factory, _sleep=fake_sleep))
        assert False, "should have raised"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1


def test_retry_loop_exhausts_budget():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _http_error(500)  # budget: 2 attempts

    async def fake_sleep(_):
        pass

    try:
        asyncio.run(lr.call_with_transient_retry(factory, _sleep=fake_sleep))
        assert False, "should have raised"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_llm_retry.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.llm_retry`

- [ ] **Step 3: Implement `backend/app/services/llm_retry.py`**

```python
"""Transient-retry primitives for the LLM call paths.

Spec: automation quality program Phase 1 (§4.1). Centralizes what used to
be ad-hoc: status-code-first classification (via ``api_error_classifier``),
Retry-After honoring, exponential backoff with full jitter, and a bounded
async retry loop. UNKNOWN errors are fail-safe (no retry storms).
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Optional, TypeVar

from app.services.api_error_classifier import (
    ClassifiedError,
    FailoverReason,
    classify_api_error,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_REASONS: frozenset = frozenset({
    FailoverReason.rate_limit,
    FailoverReason.upstream_rate_limit,
    FailoverReason.overloaded,
    FailoverReason.server_error,
    FailoverReason.timeout,
    FailoverReason.connection_error,
})

_MAX_ATTEMPTS_CAP = 3
_MAX_DELAY_SECONDS = 30.0


def is_transient(ce: ClassifiedError) -> bool:
    """True when the classified error is worth a backoff retry."""
    return ce.retryable and ce.reason in TRANSIENT_REASONS


def extract_status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Parse the Retry-After response header (delta-seconds or HTTP-date)."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(raw)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def max_attempts_for(ce: ClassifiedError) -> int:
    """Attempt budget: the classifier's per-error max_retries, capped at 3."""
    return max(1, min(_MAX_ATTEMPTS_CAP, ce.max_retries or 1))


def next_backoff(attempt: int, retry_after: Optional[float] = None) -> float:
    """Delay before retry ``attempt`` (0-based). Retry-After wins; otherwise
    exponential ``min(30, 2**attempt)`` with full jitter."""
    if retry_after is not None:
        return min(_MAX_DELAY_SECONDS, retry_after + random.uniform(0.0, 1.0))
    base = min(_MAX_DELAY_SECONDS, 2.0 ** attempt)
    return random.uniform(0.0, base)


async def call_with_transient_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    on_retry: Optional[Callable[[int, float, ClassifiedError], Awaitable[None]]] = None,
    _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``factory()`` retrying classified-transient errors with backoff.

    ``factory`` is invoked fresh per attempt. ``on_retry(next_attempt,
    delay, ce)`` fires before each backoff sleep (``next_attempt`` is the
    1-based number of the upcoming attempt). Non-transient errors raise
    immediately; transient errors raise once the budget is exhausted.
    """
    attempt = 1
    while True:
        try:
            return await factory()
        except Exception as e:
            ce = classify_api_error(e, status_code=extract_status_code(e))
            if not is_transient(ce) or attempt >= max_attempts_for(ce):
                raise
            delay = next_backoff(attempt - 1, retry_after_seconds(e))
            logger.warning(
                "llm_retry: transient %s on attempt %d; retrying in %.1fs",
                ce.reason.value, attempt, delay,
            )
            if on_retry is not None:
                await on_retry(attempt + 1, delay, ce)
            await _sleep(delay)
            attempt += 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_llm_retry.py -v`
Expected: 8 passed

---

### Task 2: Executor `_is_transient_error` classifier swap

**Files:**
- Modify: `backend/app/services/automation_executor.py:271-303` (`_TRANSIENT_PATTERNS` + `_is_transient_error`)
- Test: `backend/tests/test_transient_classification.py`

**Interfaces:**
- Consumes: `is_transient` (Task 1), `classify_api_error` (existing).
- Produces: same `_is_transient_error(error) -> bool` contract.

- [ ] **Step 1: Write the failing test**

```python
"""_is_transient_error: classifier-based, no bare-4xx-substring false blocks."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.automation_executor import _is_transient_error


def test_substring_400_no_longer_blocks_transient():
    # The old substring blocklist saw "400" anywhere and returned False even
    # when the real failure was a dropped connection.
    assert _is_transient_error(
        Exception("Exported 400 rows, then connection reset by peer")
    )


def test_explicit_permanent_codes_stay_non_transient():
    assert not _is_transient_error(Exception("HTTP 401 unauthorized"))
    assert not _is_transient_error(Exception("402 payment required"))
    assert not _is_transient_error(ValueError("invalid prompt: empty"))
    assert not _is_transient_error(PermissionError("403 forbidden: no access"))


def test_driver_level_db_drops_are_transient():
    assert _is_transient_error(Exception("database is locked"))
    assert _is_transient_error(Exception("OperationalError: server closed the connection"))


def test_existing_contract_unchanged():
    assert _is_transient_error(Exception("server returned 503 service unavailable"))
    assert _is_transient_error(Exception("connection timed out after 30s"))
    assert _is_transient_error(Exception("429 rate limit exceeded"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_transient_classification.py -v`
Expected: FAIL — `test_substring_400_no_longer_blocks_transient` (old blocklist).

- [ ] **Step 3: Implement**

In `backend/app/services/automation_executor.py`, replace `_TRANSIENT_PATTERNS` (lines 271-284) and `_is_transient_error` (lines 287-303):

```python
# Driver-level DB drops the API error classifier has no markers for. Checked
# only after the classifier says non-transient, so they can't widen 4xx.
_EXTRA_TRANSIENT_MARKERS = (
    "operationalerror", "database is locked",
    "server closed the connection", "connection lost",
)


def _is_transient_error(error: BaseException) -> bool:
    """Return True if ``error`` is a transient failure worth retrying.

    Status-code-first classification via ``api_error_classifier`` (replaces
    the old substring blocklist that matched "400" anywhere — e.g. "Exported
    400 rows" read as HTTP 400). A small marker list covers driver-level DB
    drops the classifier doesn't know. UNKNOWN is fail-safe (no retry).
    """
    from app.services.api_error_classifier import classify_api_error
    from app.services.llm_retry import is_transient

    if is_transient(classify_api_error(error)):
        return True
    text = (str(error) or "").lower()
    return any(p in text for p in _EXTRA_TRANSIENT_MARKERS)
```

(`classify_api_error` already extracts status codes from exception attributes internally.)

- [ ] **Step 4: Run test to verify it passes (+ contract regression)**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_transient_classification.py tests/test_automation_runtime_agent.py -v`
Expected: all pass (4 new + existing runtime-agent file incl. the 5 contract assertions).

---

### Task 3: Non-streaming transient retry (agents.py)

**Files:**
- Modify: `backend/app/routers/agents.py` (import ~line 67; branch at 2608-2612)

**Interfaces:**
- Consumes: `call_with_transient_retry`, `is_transient` (Task 1).

- [ ] **Step 1: Add the import**

Near line 67 (`from app.services.api_error_classifier import classify_api_error, FailoverReason`):

```python
from app.services.llm_retry import call_with_transient_retry, is_transient
```

- [ ] **Step 2: Replace the log-and-break branch (lines 2608-2612)**

```python
                elif is_transient(ce):
                    # P1.1/Phase-1: transient error — bounded backoff retry
                    # (status-code classified, Retry-After honored) instead
                    # of failing the whole turn on a single 429/503.
                    async def _retry_llm_call():
                        prune_tool_results_only(llm_messages)
                        sanitize_messages(llm_messages)
                        return await _call_llm_with_tools(
                            llm_messages, tools, tool_choice=tool_choice
                        )
                    try:
                        llm_response = await call_with_transient_retry(_retry_llm_call)
                    except Exception as retry_err:
                        ce_retry = classify_api_error(retry_err)
                        logger.warning(
                            "Transient LLM error persisted (%s) in conversation %s: %s",
                            ce_retry.reason.value, conversation_id, ce_retry.message,
                        )
                        assistant_content = (
                            f"Sorry, the AI service is temporarily unavailable "
                            f"({ce_retry.message}). Please try again."
                        )
                        break
```

(Note: the retry budget starts fresh here — the initial failure happened outside the helper, so worst case is 1 + 3 attempts, bounded.)

- [ ] **Step 3: Verify**

Run: `cd /root/zhanlu/backend && python3 -m py_compile app/routers/agents.py && echo OK`
Expected: OK. (Behavioral coverage lives in Task 1's loop tests; agents.py's full endpoint test is deferred per RAM constraint — same convention as Phase 1 RBAC.)

---

### Task 4: Streaming retry + classifier hints (agents.py)

**Files:**
- Modify: `backend/app/routers/agents.py` (stream consume ~5592-5617; error handler ~5618-5671)

**Interfaces:**
- Consumes: `is_transient`, `max_attempts_for`, `next_backoff`, `retry_after_seconds` (Task 1). Import line from Task 3 extends to: `from app.services.llm_retry import call_with_transient_retry, is_transient, max_attempts_for, next_backoff, retry_after_seconds`.
- Produces: SSE event `{"type": "llm_retry", "attempt": int, "max_attempts": int, "delay_ms": int, "reason": str}` — additive; frontend ignores unknown types.

- [ ] **Step 1: Extend the Task 3 import**

```python
from app.services.llm_retry import (
    call_with_transient_retry,
    is_transient,
    max_attempts_for,
    next_backoff,
    retry_after_seconds,
)
```

- [ ] **Step 2: Wrap the stream consume in a retry loop**

Replace the streaming branch (currently):

```python
                if streaming_enabled:
                    # ... comment block ...
                    async for ev_type, ev_data in _stream_llm_with_tools(
                        llm_messages, tools, tool_choice=tool_choice,
                    ):
                        if ev_type == "delta":
                            ...
                        elif ev_type == "error":
                            raise RuntimeError(ev_data)
```

with:

```python
                if streaming_enabled:
                    # ── Gap 1: true token streaming ───────────────────
                    # (existing comment block unchanged)
                    # Phase 1: bounded transient retry. Only safe BEFORE the
                    # first client-visible chunk (content_streamed) — after
                    # that, a restart would duplicate emitted content, so we
                    # raise into the outer handler as before.
                    _stream_attempt = 1
                    while True:
                        try:
                            async for ev_type, ev_data in _stream_llm_with_tools(
                                llm_messages, tools, tool_choice=tool_choice,
                            ):
                                if ev_type == "delta":
                                    assistant_content += ev_data
                                    content_streamed = True
                                    yield f'data: {json.dumps({"type": "delta", "content": ev_data})}\n\n'
                                elif ev_type == "reasoning":
                                    final_reasoning += ev_data
                                elif ev_type == "tool_calls":
                                    raw_tool_calls = ev_data or []
                                elif ev_type == "done":
                                    if ev_data and not assistant_content:
                                        assistant_content = ev_data
                                elif ev_type == "error":
                                    raise RuntimeError(ev_data)
                            break  # stream completed cleanly
                        except Exception as stream_err:
                            ce_stream = classify_api_error(stream_err)
                            if (
                                content_streamed
                                or not is_transient(ce_stream)
                                or _stream_attempt >= max_attempts_for(ce_stream)
                            ):
                                raise
                            delay = next_backoff(
                                _stream_attempt - 1, retry_after_seconds(stream_err)
                            )
                            _stream_attempt += 1
                            logger.warning(
                                "stream: transient %s in conversation %s; "
                                "retry %d/%d in %.1fs",
                                ce_stream.reason.value, conversation_id,
                                _stream_attempt, max_attempts_for(ce_stream), delay,
                            )
                            yield f'data: {json.dumps({"type": "llm_retry", "attempt": _stream_attempt, "max_attempts": max_attempts_for(ce_stream), "delay_ms": int(delay * 1000), "reason": ce_stream.reason.value})}\n\n'
                            await asyncio.sleep(delay)
                else:
                    # ── Legacy buffered path ──────────────────────────
                    llm_response = await _call_llm_with_tools(
                        llm_messages, tools, tool_choice=tool_choice,
                    )
                    assistant_content = llm_response.get("content", "")
                    raw_tool_calls = llm_response.get("tool_calls", [])
                    final_reasoning = llm_response.get("reasoning", "") or ""
```

- [ ] **Step 3: Honor should_fallback / should_compress in the stream error handler**

In the `except Exception as e:` block (currently: classify → log → `metrics.record_error` → persist error → yield error event → return), insert hint handling between `metrics.record_error(ce.reason.value)` and the persistence block:

```python
                metrics.record_error(ce.reason.value)
                # Phase 1: honor classifier hints the stream path used to drop.
                # Only safe before any client-visible content was emitted.
                if not content_streamed and ce.should_fallback:
                    # Switch model for a final buffered attempt. Seamless:
                    # nothing has streamed yet, so emitting the buffered
                    # content as one delta is indistinguishable to the client.
                    try:
                        async def _call_with_fallback_model(_model_name):
                            return await _call_llm_with_tools(
                                llm_messages, tools, tool_choice=tool_choice,
                                model_override=_model_name,
                            )
                        _fb_response, _fb_model = await with_fallback(
                            get_model(), _call_with_fallback_model, ce
                        )
                    except Exception as _fb_err:
                        _fb_response, _fb_model = None, None
                        logger.warning("stream: fallback attempts failed: %s", _fb_err)
                    if _fb_response:
                        assistant_content = _fb_response.get("content", "")
                        raw_tool_calls = _fb_response.get("tool_calls", [])
                        final_reasoning = _fb_response.get("reasoning", "") or ""
                        logger.info("stream: fallback to model %s succeeded", _fb_model)
                        if assistant_content:
                            content_streamed = True
                            yield f'data: {json.dumps({"type": "delta", "content": assistant_content})}\n\n'
                        # Fall through to the post-try flow (reasoning_done,
                        # tool-call handling) instead of failing the turn.
                    else:
                        yield from _persist_stream_error(
                            db, conv, conversation_id, messages, e, ce,
                            tool_calls_for_frontend, artifact_ids,
                        )
                        _discard_steer(conversation_id)
                        return
                elif not content_streamed and ce.should_compress and not _stream_compaction_attempted:
                    # Context overflow: one reactive compaction pass, then
                    # re-enter the iteration with compacted messages.
                    _stream_compaction_attempted = True
                    try:
                        from app.services.compaction import auto_compact_if_needed
                        llm_messages, was_compacted = await auto_compact_if_needed(
                            llm_messages,
                            model=get_model(),
                            state=_compaction_states.get(conversation_id, AutoCompactState()),
                            force=True,
                            trigger="reactive",
                        )
                    except Exception as compact_err:
                        logger.error("stream: reactive compaction failed: %s", compact_err)
                        was_compacted = False
                    if was_compacted:
                        if not llm_messages or llm_messages[0].get("role") != "system":
                            llm_messages.insert(0, {"role": "system", "content": system_prompt})
                        continue
                    yield from _persist_stream_error(
                        db, conv, conversation_id, messages, e, ce,
                        tool_calls_for_frontend, artifact_ids,
                    )
                    _discard_steer(conversation_id)
                    return
                else:
                    yield from _persist_stream_error(
                        db, conv, conversation_id, messages, e, ce,
                        tool_calls_for_frontend, artifact_ids,
                    )
                    _discard_steer(conversation_id)
                    return
```

Refactor the existing persistence block (the `try: err_msg = {...} ... db.rollback()` + `yield error` lines) into a module-level generator so the three call sites share it:

```python
def _persist_stream_error(db, conv, conversation_id, messages, e, ce,
                          tool_calls_for_frontend, artifact_ids):
    """Persist an assistant error message so the conversation isn't left
    half-baked (user message saved, no assistant reply), then yield the SSE
    error event. Extracted from the v3 stream handler in Phase 1 — behavior
    unchanged."""
    assistant_content = f"Sorry, I encountered an error: {ce.message}"
    try:
        err_msg = {
            "id": str(uuid.uuid4()), "role": "assistant",
            "content": f"**[Error]** {assistant_content}",
            "created_date": datetime.utcnow().isoformat(),
        }
        if tool_calls_for_frontend:
            err_msg["tool_calls"] = tool_calls_for_frontend
        if artifact_ids:
            err_msg["artifact_ids"] = list(artifact_ids)
        messages.append(err_msg)
        conv.messages = list(messages)
        conv.updated_date = datetime.utcnow()
        db.commit()
        try:
            conv = db.query(AgentConversation).filter(
                AgentConversation.id == conversation_id,
            ).first() or conv
        except Exception:
            pass
    except Exception as _persist_err:
        logger.warning("v3 stream: failed to persist error message: %s", _persist_err)
        db.rollback()
    yield f'data: {json.dumps({"type": "error", "message": str(e), "conversation": conv.to_dict()})}\n\n'
```

Initialize `_stream_compaction_attempted = False` next to the per-turn state (where `guardrail_retries` is initialized, before the iteration loop).

- [ ] **Step 4: Verify**

Run: `cd /root/zhanlu/backend && python3 -m py_compile app/routers/agents.py && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_v3_stream_content_streamed_init.py -v`
Expected: OK + 1 passed (stream-init regression).

---

### Task 5: `call_llm` failover gating

**Files:**
- Modify: `backend/app/services/llm_service.py:196-214`
- Test: `backend/tests/test_call_llm_failover.py`

**Interfaces:**
- Consumes: `classify_api_error`, `is_transient` semantics inline (permanent → raise).

- [ ] **Step 1: Write the failing test**

```python
"""call_llm: permanent 4xx fails fast (no pointless failover); transient
errors still fail over to the next provider."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import httpx

from app.services import llm_service


def _providers():
    return [
        SimpleNamespace(name="p1", base_url="http://p1", api_key="k1", model="m1"),
        SimpleNamespace(name="p2", base_url="http://p2", api_key="k2", model="m2"),
    ]


def _http_error(status):
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=httpx.Request("POST", "http://x"),
        response=httpx.Response(status),
    )


class _FakeClient:
    """AsyncContextManager mimicking httpx.AsyncClient.post per-URL."""
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(url)
        outcome = self.behavior[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok_response(model="m2"):
    resp = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "hi"}}], "usage": {}},
        request=httpx.Request("POST", "http://x"),
    )
    return resp


def test_permanent_400_raises_without_failover():
    client = _FakeClient({
        "http://p1/chat/completions": _http_error(400),
        "http://p2/chat/completions": _ok_response(),
    })
    with patch.object(llm_service, "get_llm_providers", return_value=_providers()), \
         patch.object(llm_service.httpx, "AsyncClient", lambda timeout: client):
        try:
            asyncio.run(llm_service.call_llm(prompt="x"))
            assert False, "should have raised"
        except Exception:
            pass
    assert client.calls == ["http://p1/chat/completions"]  # no failover on 4xx


def test_transient_503_fails_over():
    client = _FakeClient({
        "http://p1/chat/completions": _http_error(503),
        "http://p2/chat/completions": _ok_response(),
    })
    with patch.object(llm_service, "get_llm_providers", return_value=_providers()), \
         patch.object(llm_service.httpx, "AsyncClient", lambda timeout: client):
        result = asyncio.run(llm_service.call_llm(prompt="x"))
    assert client.calls == ["http://p1/chat/completions", "http://p2/chat/completions"]
    assert result["response"] == "hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_call_llm_failover.py -v`
Expected: FAIL — 400 currently fails over to p2 (returns "hi") instead of raising.

- [ ] **Step 3: Implement**

In `llm_service.py`, replace the `except` block in the provider loop (lines 207-214):

```python
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            from app.services.api_error_classifier import classify_api_error
            from app.services.llm_retry import is_transient
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            ce = classify_api_error(e, status_code=status)
            logger.warning(
                "call_llm: provider '%s' failed (%s, reason=%s): %s",
                provider.name, type(e).__name__, ce.reason.value, e,
            )
            last_error = e
            _llm_error = str(e)
            if not is_transient(ce):
                # Permanent failure (bad request, auth, billing, …): trying
                # the next provider would reproduce the same error and mask
                # the real one. Fail fast.
                raise
            continue
```

(Note: `_llm_error` assignment moves above the branch so both paths record it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_call_llm_failover.py -v`
Expected: 2 passed

---

### Task 6: Data-source connectivity preflight

**Files:**
- Modify: `backend/app/services/automation_executor.py` (new helpers + gate after line 645)
- Modify: `backend/app/config.py` (new setting)
- Test: `backend/tests/test_ds_connectivity_preflight.py`

**Interfaces:**
- Produces: `ax._check_bound_source_connectivity(db, bound_ids, timeout_seconds=None) -> list[dict]` — failures as `{"kb_id", "name", "target", "error", "kind"}` with `kind in {"unreachable", "misconfigured"}`. Consumed by the data_sync gate in `execute_automation`.

- [ ] **Step 1: Write the failing test**

```python
"""Connectivity preflight: bound sources are probed before spending a run."""
import os
import sqlite3
import sys
import time
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services import automation_executor as ax


def _kb(**kw):
    defaults = dict(id=str(uuid.uuid4()), name="KB", type="database",
                    source_kind="database", is_deleted=False)
    defaults.update(kw)
    return KnowledgeBase(**defaults)


def test_sqlite_source_passes(tmp_path):
    db_file = tmp_path / "ok.sqlite"
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()
    db = SessionLocal()
    try:
        kb = _kb(db_type="sqlite", api_url=str(db_file), file_url=str(db_file))
        db.add(kb); db.commit()
        assert ax._check_bound_source_connectivity(db, [kb.id]) == []
    finally:
        db.rollback(); db.close()


def test_unreachable_db_source_fails_fast_and_bounded():
    db = SessionLocal()
    try:
        kb = _kb(db_type="mysql", host="10.255.255.1", port=3306,
                 database_name="erp", username="u", password="p")
        db.add(kb); db.commit()
        start = time.monotonic()
        failures = ax._check_bound_source_connectivity(db, [kb.id], timeout_seconds=3.0)
        elapsed = time.monotonic() - start
        assert len(failures) == 1
        assert failures[0]["kind"] == "unreachable"
        assert "10.255.255.1" in failures[0]["target"]
        assert elapsed < 10  # bounded by the wrapper, not the driver default
    finally:
        db.rollback(); db.close()


def test_deleted_source_is_misconfigured():
    db = SessionLocal()
    try:
        kb = _kb(db_type="mysql", host="x", is_deleted=True)
        db.add(kb); db.commit()
        failures = ax._check_bound_source_connectivity(db, [kb.id])
        assert failures[0]["kind"] == "misconfigured"
    finally:
        db.rollback(); db.close()


def test_file_sources_are_skipped():
    db = SessionLocal()
    try:
        kb = _kb(source_kind="file", db_type=None, api_url=None,
                 file_url="/nonexistent/file.pdf")
        db.add(kb); db.commit()
        assert ax._check_bound_source_connectivity(db, [kb.id]) == []
    finally:
        db.rollback(); db.close()


def test_executor_calls_preflight_before_agent_run():
    # Source-literal gate, same style as test_automation_ds_preflight.py.
    src = open(os.path.join(_BACKEND_ROOT, "app/services/automation_executor.py")).read()
    gate = src.index("_check_bound_source_connectivity(")
    run = src.index("pool.submit(_run_agent_in_conversation")
    assert gate < run
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_ds_connectivity_preflight.py -v`
Expected: FAIL — `AttributeError: _check_bound_source_connectivity`.

- [ ] **Step 3: Implement**

`backend/app/config.py` — after `AUTOMATION_RUN_TIMEOUT_SECONDS` (or nearby automation settings):

```python
    AUTOMATION_DS_PREFLIGHT_TIMEOUT_SECONDS: float = 8.0
```

`backend/app/services/automation_executor.py` — new helpers near `_resolve_bound_data_source_ids`:

```python
def _probe_db_source(kb, timeout_seconds: float) -> dict | None:
    """SELECT 1 through the canonical connector layer, bounded by a wrapper
    timeout (driver defaults are 10s+). Returns a failure dict or None."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    from app.services.db.connector_factory import DriverUnavailable, get_connector

    def _probe():
        with get_connector(kb) as conn:
            conn.execute("SELECT 1")

    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(_probe)
    try:
        fut.result(timeout=timeout_seconds)
        return None
    except FuturesTimeout:
        return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                "target": f"{kb.host}:{kb.port}",
                "error": f"connection probe timed out after {timeout_seconds}s"}
    except DriverUnavailable as e:
        return {"kb_id": kb.id, "name": kb.name, "kind": "misconfigured",
                "target": f"{kb.host}:{kb.port}", "error": str(e)}
    except Exception as e:
        return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                "target": f"{kb.host}:{kb.port}", "error": str(e)[:200]}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _probe_api_source(kb, timeout_seconds: float) -> dict | None:
    """HEAD (fallback GET) an API-bound source. Failure dict or None."""
    import httpx
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            try:
                resp = client.head(kb.api_url)
                if resp.status_code in (405, 501):
                    resp = client.get(kb.api_url)
            except httpx.RequestError:
                resp = client.get(kb.api_url)
        if resp.status_code >= 500:
            return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                    "target": kb.api_url, "error": f"HTTP {resp.status_code}"}
        return None
    except Exception as e:
        return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                "target": kb.api_url, "error": str(e)[:200]}


def _check_bound_source_connectivity(
    db: Session, bound_ids: list, timeout_seconds: float | None = None,
) -> list[dict]:
    """Probe every bound data source before a data_sync run spends an LLM
    turn. Returns [] when all reachable; otherwise failure dicts
    (kind="unreachable" is retryable, "misconfigured" is not).
    """
    from app.models.knowledge_base import KnowledgeBase

    timeout = timeout_seconds or getattr(
        settings, "AUTOMATION_DS_PREFLIGHT_TIMEOUT_SECONDS", 8.0
    )
    failures: list[dict] = []
    for kb_id in bound_ids:
        kb = db.get(KnowledgeBase, kb_id)
        if kb is None or kb.is_deleted:
            failures.append({
                "kb_id": kb_id, "name": kb_id, "kind": "misconfigured",
                "target": "", "error": "bound data source was deleted",
            })
            continue
        if (kb.source_kind or "").lower() == "file" or (
            not kb.db_type and not kb.api_url
        ):
            continue  # file/manual sources need no connectivity probe
        failure = (
            _probe_db_source(kb, timeout)
            if kb.db_type
            else _probe_api_source(kb, timeout)
        )
        if failure:
            failures.append(failure)
    return failures
```

Wire the gate in `execute_automation` — immediately after the `if not _bound_ids:` block (line 645), still inside the `data_sync` branch:

```python
            _ds_failures = _check_bound_source_connectivity(db, _bound_ids)
            if _ds_failures:
                _detail = "; ".join(
                    f"{f['name']} ({f['target']}): {f['error']}"
                    for f in _ds_failures[:3]
                )
                if all(f["kind"] == "misconfigured" for f in _ds_failures):
                    _mark_failed_no_retry(
                        db, execution,
                        f"Data source misconfigured — {_detail}",
                    )
                else:
                    # Retryable: schedule_retry backoff handles a source
                    # that comes back later.
                    _mark_failed(
                        db, execution,
                        f"Data source unreachable — {_detail}",
                    )
                return
```

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2
Expected: 5 passed

---

### Task 7: Phase-1 verification sweep

- [ ] **Step 1: Backend — all new + regression files**

Run:
```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && \
DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && \
DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
  tests/test_llm_retry.py \
  tests/test_transient_classification.py \
  tests/test_call_llm_failover.py \
  tests/test_ds_connectivity_preflight.py \
  tests/test_automation_runtime_agent.py \
  tests/test_automation_ds_preflight.py \
  tests/test_automation_truthful_failure.py \
  tests/test_execute_automation_poll.py \
  tests/test_v3_stream_content_streamed_init.py \
  tests/test_automation_phase5_quality_and_delta.py \
  -v
```
Expected: all pass (8 + 4 + 2 + 5 + regressions).

- [ ] **Step 2: Compile + lint**

Run: `cd /root/zhanlu/backend && python3 -m py_compile app/services/llm_retry.py app/services/llm_service.py app/services/automation_executor.py app/routers/agents.py app/config.py && echo OK`
Expected: OK.

## Self-Review

- **Spec coverage:** §4.1 retry (Tasks 1, 3, 4) ✓; stream hints retryable/fallback/compress (Task 4) ✓; non-streaming parity (Task 3) ✓; `call_llm` 4xx gating (Task 5) ✓; `_is_transient_error` substring replacement (Task 2) ✓; §4.2 connectivity preflight (Task 6) ✓ — DB `SELECT 1` ✓, API HEAD/GET ✓, fail fast + `schedule_retry` via `_mark_failed` ✓, misconfigured → no-retry ✓.
- **Placeholder scan:** all code complete. The agents.py stream event body is copied verbatim from the current source, not referenced.
- **Type consistency:** `call_with_transient_retry(factory, *, on_retry, _sleep)` identical in Tasks 1/3; `_persist_stream_error(db, conv, conversation_id, messages, e, ce, tool_calls_for_frontend, artifact_ids)` — 3 call sites match the signature; `is_transient(ce)` takes ClassifiedError everywhere (Task 5 passes `ce`, correct).
- **Known boundaries:** agents.py integration verified by py_compile + existing stream tests (endpoint-level test deferred — RAM constraint, same convention as Phase 1 RBAC and noted in the spec's testing section). Retry event consumed by no frontend yet (additive, safe).
