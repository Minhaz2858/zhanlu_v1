"""Turn diagnostics — Stage 0 (observability only, 2026-10-27).

WHY THIS MODULE EXISTS
----------------------
The 2026-10-27 answer-collapse investigation could not answer basic questions
from production logs:

  * Did a stream end by normal completion, an exception, cancellation, or a
    client disconnect?  (No exit-reason log existed anywhere.)
  * Which post-loop guard fired for THIS turn, and did it clear, replace or
    preserve the answer?
  * Can one join ``chat_messages``, ``executions`` and ``agent_invocations``
    for a single user turn?  (No turn identifier existed.)

Without those facts every proposed root cause is a hypothesis. This module
adds the facts. It is deliberately *inert*: see the INVARIANTS section.

INVARIANTS — Stage 0 is observability ONLY
------------------------------------------
Everything here is designed so that importing/using it cannot change product
behaviour:

  1. Every public function returns ``None`` or a plain value and NEVER raises:
     each body is wrapped in ``try/except BaseException``.
  2. Nothing here mutates shared application state, model rows, DB sessions,
     response payloads, or SSE frames. There is no ``db`` parameter.
  3. The only side effects are ``logging`` calls and increments of a private
     ``Counter``.
  4. ``turn_scope()`` only *sets* a contextvar and restores the previous value
     on exit — the same pattern as the existing ``TraceContext``.
  5. No content is logged. Only lengths, booleans, enums and ids.

PRIVACY
-------
Never log: credentials, tokens, raw database rows, SQL result values, or
message bodies. Only lengths / counts / identifiers. ``_short()`` truncates
identifiers used purely for correlation.

USAGE
-----
    from app.services.turn_diagnostics import (
        turn_scope, current_turn_id, note_guard, note_stream_exit,
        note_terminal, time_finalization,
    )

    with turn_scope(conversation_id=conv_id) as turn_id:
        ...                       # the whole turn
        note_guard("promise_strip", action="regenerated", before_len=1200)
        note_stream_exit("normal")
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from collections import Counter
from typing import Any, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "turn_scope",
    "current_turn_id",
    "get_turn_id",
    "note_guard",
    "note_stream_exit",
    "note_terminal",
    "note_iteration",
    "note_tool_execution",
    "time_finalization",
    "snapshot_counters",
    "reset_counters",
]


# ── Turn identity ────────────────────────────────────────────────────────
# ONE canonical id per user turn (see design doc §4). Minted on the server at
# request entry; a client-supplied value is recorded for comparison but never
# becomes canonical. Propagated by contextvar: verified to survive
# ``asyncio.to_thread`` (the FSM runs there) and asyncio task creation.
_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "v3_turn_id", default=None
)
_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "v3_turn_conversation_id", default=None
)
_client_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "v3_turn_client_id", default=None
)


def _short(value: Any, n: int = 8) -> str:
    """Truncate an identifier for logs. Never raises."""
    try:
        s = str(value) if value is not None else "-"
        return s[:n]
    except BaseException:  # noqa: BLE001 — diagnostics must never raise
        return "-"


def get_turn_id() -> str:
    """Current turn id, minting one if absent (safe fallback)."""
    try:
        tid = _turn_id.get()
        if tid:
            return tid
        tid = uuid.uuid4().hex
        _turn_id.set(tid)
        return tid
    except BaseException:  # noqa: BLE001
        return "-"


#: Public alias — identical behaviour, clearer call sites.
current_turn_id = get_turn_id


class _TurnScope:
    """Context manager: establish the canonical turn id, restore on exit."""

    __slots__ = ("conversation_id", "client_turn_id", "turn_id",
                 "_tok_turn", "_tok_conv", "_tok_client", "_started")

    def __init__(self, conversation_id: Any = None,
                 client_turn_id: Any = None) -> None:
        self.conversation_id = conversation_id
        self.client_turn_id = client_turn_id
        self.turn_id = uuid.uuid4().hex
        self._tok_turn = None
        self._tok_conv = None
        self._tok_client = None
        self._started = time.monotonic()

    def __enter__(self) -> str:
        try:
            self._tok_turn = _turn_id.set(self.turn_id)
            self._tok_conv = _conversation_id.set(
                str(self.conversation_id) if self.conversation_id else None)
            self._tok_client = _client_turn_id.set(
                str(self.client_turn_id) if self.client_turn_id else None)
            logger.info(
                "turn_diag: turn started turn=%s conv=%s client_turn=%s",
                self.turn_id, _short(self.conversation_id),
                _short(self.client_turn_id) if self.client_turn_id else "-",
            )
        except BaseException:  # noqa: BLE001
            pass
        return self.turn_id

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Log turn duration + uncaught exception class. Never suppresses."""
        try:
            dur_ms = int((time.monotonic() - self._started) * 1000)
            logger.info(
                "turn_diag: turn scope exited turn=%s conv=%s dur_ms=%d exc=%s",
                self.turn_id, _short(self.conversation_id), dur_ms,
                getattr(exc_type, "__name__", "none") if exc_type else "none",
            )
        except BaseException:  # noqa: BLE001
            pass
        # Restore contextvars in reverse order; ignore restore failures.
        for var, tok in ((_client_turn_id, self._tok_client),
                         (_conversation_id, self._tok_conv),
                         (_turn_id, self._tok_turn)):
            try:
                if tok is not None:
                    var.reset(tok)
            except BaseException:  # noqa: BLE001
                pass
        return False  # never suppress the exception


def turn_scope(conversation_id: Any = None,
               client_turn_id: Any = None) -> _TurnScope:
    """Open a turn scope. Usage: ``with turn_scope(conv_id) as turn_id:``."""
    return _TurnScope(conversation_id=conversation_id,
                      client_turn_id=client_turn_id)


# ── Counters (process-local; read via snapshot_counters) ─────────────────
_counters: Counter = Counter()


def _bump(key: str) -> None:
    try:
        _counters[key] += 1
    except BaseException:  # noqa: BLE001
        pass


# ── Structured observations ──────────────────────────────────────────────


def note_guard(
    guard: str,
    *,
    action: str = "unknown",
    before_len: int | None = None,
    after_len: int | None = None,
    iteration: int | None = None,
    replacement_pending: bool | None = None,
) -> None:
    """Record that a post-loop/in-loop guard fired for this turn.

    ``action`` is one of: ``cleared`` | ``regenerated`` | ``preserved`` |
    ``replaced`` | ``rejected`` | ``stripped`` | ``noop``.
    Only LENGTHS are recorded — never the text.
    """
    try:
        _bump(f"guard.{guard}")
        _bump(f"guard_action.{action}")
        logger.info(
            "turn_diag: guard fired turn=%s guard=%s action=%s iter=%s "
            "before_len=%s after_len=%s replacement_pending=%s",
            get_turn_id(), guard, action,
            iteration if iteration is not None else "-",
            before_len if before_len is not None else "-",
            after_len if after_len is not None else "-",
            replacement_pending if replacement_pending is not None else "-",
        )
    except BaseException:  # noqa: BLE001
        pass


def note_stream_exit(
    reason: str,
    *,
    terminal_seen: bool | None = None,
    exception: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Record WHY a stream generator ended.

    ``reason``: ``normal`` | ``exception`` | ``cancelled`` | ``generator_exit``
    | ``client_disconnect`` | ``watchdog`` | ``pump_error``.
    This is the fact the 2026-10-27 investigation could not obtain.
    """
    try:
        _bump(f"stream_exit.{reason}")
        logger.info(
            "turn_diag: stream exit turn=%s reason=%s terminal_seen=%s "
            "exception=%s dur_ms=%s",
            get_turn_id(), reason,
            terminal_seen if terminal_seen is not None else "-",
            exception or "-",
            duration_ms if duration_ms is not None else "-",
        )
    except BaseException:  # noqa: BLE001
        pass


def note_terminal(
    kind: str,
    *,
    content_len: int | None = None,
    persist_status: str | None = None,
) -> None:
    """Record the terminal frame emitted (kind: done/error/paused/...)."""
    try:
        _bump(f"terminal.{kind}")
        logger.info(
            "turn_diag: terminal turn=%s kind=%s content_len=%s persist=%s",
            get_turn_id(), kind,
            content_len if content_len is not None else "-",
            persist_status or "-",
        )
    except BaseException:  # noqa: BLE001
        pass


def note_iteration(
    index: int,
    *,
    content_len: int = 0,
    has_tool_calls: bool = False,
    finish_reason: str | None = None,
) -> None:
    """Record one LLM iteration of the ReAct loop (counts/lengths only)."""
    try:
        _bump("iteration")
        logger.info(
            "turn_diag: iteration turn=%s i=%d content_len=%d tools=%s finish=%s",
            get_turn_id(), index, content_len, has_tool_calls,
            finish_reason or "-",
        )
    except BaseException:  # noqa: BLE001
        pass


def note_tool_execution(
    tool_name: str,
    *,
    ok: bool | None = None,
    execution_id: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Correlate a tool call (and its FSM execution id) with this turn."""
    try:
        _bump(f"tool.{tool_name}")
        logger.info(
            "turn_diag: tool turn=%s name=%s ok=%s exec=%s dur_ms=%s",
            get_turn_id(), tool_name,
            ok if ok is not None else "-",
            _short(execution_id) if execution_id else "-",
            duration_ms if duration_ms is not None else "-",
        )
    except BaseException:  # noqa: BLE001
        pass


class _Timed:
    """Timer for one finalization phase; logs on exit, never raises."""

    __slots__ = ("phase", "_t0")

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self._t0 = time.monotonic()

    def __enter__(self) -> "_Timed":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            dur = int((time.monotonic() - self._t0) * 1000)
            _bump(f"finalize.{self.phase}")
            logger.info(
                "turn_diag: finalize phase=%s turn=%s dur_ms=%d exc=%s",
                self.phase, get_turn_id(), dur,
                getattr(exc_type, "__name__", "none") if exc_type else "none",
            )
        except BaseException:  # noqa: BLE001
            pass
        return False


def time_finalization(phase: str) -> _Timed:
    """Time a finalization phase: reconcile | persist | terminal | post_persist."""
    return _Timed(phase)


def snapshot_counters() -> dict[str, int]:
    """Copy of process-local counters (for tests / admin endpoints)."""
    try:
        return dict(_counters)
    except BaseException:  # noqa: BLE001
        return {}


def reset_counters() -> None:
    """Clear counters (tests only)."""
    try:
        _counters.clear()
    except BaseException:  # noqa: BLE001
        pass


def _iter_scope_ids() -> Iterator[tuple[str, str]]:
    """(name, short value) pairs for debugging; never raises."""
    for name, var in (("turn", _turn_id), ("conv", _conversation_id),
                      ("client_turn", _client_turn_id)):
        try:
            yield name, _short(var.get())
        except BaseException:  # noqa: BLE001
            yield name, "-"
