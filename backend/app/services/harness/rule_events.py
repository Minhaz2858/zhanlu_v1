"""Rule-firing telemetry for the deterministic decision layer (Harness Phase 0).

Context
-------
~12 functions in this codebase read the user's *text* and decide agent
behaviour: force a deliverable (`should_force_create_file`), route a skill
(`detect_file_intent`), refuse to create (`is_file_read_request`). They are the
"hardcoded rules" the Agent Harness migration removes — but they cannot be
deleted safely until we can show, per rule, how often it fires and how often it
changes the outcome. This module records exactly that. See
``docs/plans/2026-09-18-agent-harness-migration.md`` (Phase 0).

Contract (non-negotiable)
------------------------
* **FAIL-OPEN.** Recording must never raise into a turn, never block, never
  change a decision. Every failure path is swallowed and counted in
  ``counters()["record_failures"]``.
* **CHEAP.** One counter increment plus one buffered row. No network, no LLM,
  no DB write on the calling thread.
* **CORRELATED.** A contextvar carries turn identity (conversation, agent,
  surface) so rows group by turn without threading arguments through 12
  signatures.

Usage
-----
    from app.services.harness.rule_events import observe_rule

    @observe_rule("file_turn_guard.should_force_create_file", forced="create_artifact")
    def should_force_create_file(user_content, tool_calls, *, iteration, ...):
        ...

    bind_turn(conversation_id=..., agent_name=..., surface="chat.v3")
    ... turn runs ...
    flush_rule_events()   # probes/tests; a daemon also flushes periodically

Env
---
``HARNESS_RULE_EVENTS_ENABLED`` (default ``1``) — master switch.
``HARNESS_RULE_EVENTS_PERSIST`` (default ``1``) — write rows to the DB.
``HARNESS_RULE_EVENTS_LOG``     (default ``0``) — also emit a JSON log line.

These read the environment directly (not Settings) so Phase 0 touches no shared
config surface; Phase 1a folds them into the harness policy object.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import uuid
from collections import Counter, deque
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_QUEUE_MAX = 1000
_FLUSH_INTERVAL_S = 5.0

_current_turn: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "harness_rule_turn", default={}
)

_COUNTS: Counter = Counter()
_LOCK = threading.Lock()
_BUFFER: deque = deque(maxlen=_QUEUE_MAX)
_DROPPED = 0
# Plain ints, deliberately NOT stored in _COUNTS: the failure counters must stay
# updatable even when the counter store itself is the thing that broke, or the
# handler would raise out of record_rule_event and break a live turn.
_RECORD_FAILURES = 0
_FLUSH_FAILURES = 0
_FLUSHER: Optional[threading.Thread] = None


# ── env helpers (read at call time so tests can monkeypatch the env) ──────────

def _enabled() -> bool:
    return os.getenv("HARNESS_RULE_EVENTS_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _persist_enabled() -> bool:
    return os.getenv("HARNESS_RULE_EVENTS_PERSIST", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _log_enabled() -> bool:
    return os.getenv("HARNESS_RULE_EVENTS_LOG", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ── turn correlation ─────────────────────────────────────────────────────────

def bind_turn(
    conversation_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    surface: Optional[str] = None,
    user_id: Optional[str] = None,
    **extra: Any,
) -> dict:
    """Attach turn identity to the current context. Returns the turn dict.

    Callers that know who is talking (the chat turn entry points) call this once;
    every rule firing afterwards inherits it. Safe to call with no arguments.
    """
    turn = {
        "conversation_id": conversation_id,
        "agent_name": agent_name,
        "surface": surface,
        "user_id": user_id,
        "turn_id": (extra.pop("turn_id", None) or str(uuid.uuid4())),
    }
    turn.update(extra)
    if _enabled():
        try:
            _current_turn.set({k: v for k, v in turn.items() if v is not None})
        except Exception:  # pragma: no cover - contextvar set is effectively infallible
            pass
    return turn


def current_turn() -> dict:
    try:
        return dict(_current_turn.get() or {})
    except Exception:  # pragma: no cover
        return {}


def clear_turn() -> None:
    try:
        _current_turn.set({})
    except Exception:  # pragma: no cover
        pass


# ── decision helpers ─────────────────────────────────────────────────────────

_FALSY = (None, False, 0, "", (), [], {}, set())


def is_fired(decision: Any) -> bool:
    """True when ``decision`` represents the rule changing the outcome.

    Handles the shapes the decision functions actually return: bool, None,
    a format string, ``(False, None)`` from ``is_file_deliverable_request`` and
    a dict from the goal-contract helpers.
    """
    if isinstance(decision, tuple):
        if not decision:
            return False
        return is_fired(decision[0])
    if isinstance(decision, dict):
        for key in ("fired", "force", "should_force", "create"):
            if key in decision:
                return is_fired(decision[key])
        return bool(decision)
    if isinstance(decision, str):
        return decision.strip().lower() not in {"", "none", "false", "no", "general"}
    return bool(decision) if decision not in _FALSY else False


def forced_from(decision: Any, forced: Optional[str] = None) -> Optional[str]:
    """What the rule forced: the caller-declared target, else the decision value."""
    if forced:
        return forced
    if isinstance(decision, tuple):
        tail = [d for d in decision if isinstance(d, str) and d]
        return tail[0] if tail else None
    if isinstance(decision, str) and decision:
        return decision
    return None


def _clip(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    return value[:limit]


# ── recording ────────────────────────────────────────────────────────────────

def record_rule_event(
    rule_name: str,
    decision: Any,
    *,
    forced: Optional[str] = None,
    detail: Optional[dict] = None,
    turn: Optional[dict] = None,
) -> None:
    """Record one rule evaluation. Never raises, never blocks."""
    global _DROPPED
    if not _enabled():
        return
    try:
        fired = is_fired(decision)
        ctx = turn if turn is not None else current_turn()
        detail_row = _merge_detail(ctx, detail)
        # Phase 2: compare this rule's decision against the model's own tool-call
        # trace for the same turn. Fail-open — a shadow failure must leave the
        # Phase 0 row exactly as it was.
        try:
            from app.services.harness.shadow import record_shadow
            view = record_shadow(rule_name, fired)
            if view:
                detail_row = dict(detail_row or {})
                detail_row.update(view)
        except Exception:
            pass
        row = {
            "id": str(uuid.uuid4()),
            "rule_name": rule_name,
            "fired": fired,
            "decision": _clip(_raw_decision(decision), 300),
            "forced_target": _clip(forced_from(decision, forced), 80),
            "conversation_id": ctx.get("conversation_id"),
            "turn_id": ctx.get("turn_id"),
            "agent_name": ctx.get("agent_name"),
            "surface": ctx.get("surface"),
            "detail": detail_row,
        }
        with _LOCK:
            _COUNTS[rule_name] += 1
            # Materialise the fired counter even at 0: "evaluated N times, never
            # fired" is the evidence Phase 3 deletes a rule on.
            if fired:
                _COUNTS[f"{rule_name}::fired"] += 1
            else:
                _COUNTS.setdefault(f"{rule_name}::fired", 0)
            if len(_BUFFER) == _QUEUE_MAX:
                _DROPPED += 1
            _BUFFER.append(row)
        if _log_enabled():
            logger.info("harness_rule_event %s", json.dumps(row, ensure_ascii=False, default=str))
        if _persist_enabled():
            _ensure_flusher()
    except Exception:
        global _RECORD_FAILURES
        _RECORD_FAILURES += 1


def _raw_decision(decision: Any) -> Any:
    """A JSON-friendly view of the decision for the evidence table."""
    if isinstance(decision, tuple):
        return [_raw_decision(d) for d in decision]
    return decision


def _merge_detail(ctx: dict, detail: Optional[dict]) -> Optional[dict]:
    merged: dict = {}
    if ctx.get("user_id"):
        merged["user_id"] = ctx["user_id"]
    for key, value in (ctx.get("extra") or {}).items():
        merged[key] = value
    if detail:
        merged.update(detail)
    return merged or None


def observe_rule(
    rule_name: str,
    *,
    forced: Optional[str] = None,
    extract: Optional[Callable[[Any], Optional[dict]]] = None,
    capture_input: bool = True,
) -> Callable:
    """Decorator: record one rule evaluation per call, pass the result through.

    ``forced`` names the target the rule can force (Phase 3 deletion evidence).
    ``extract`` may return extra detail from the result (e.g. matched fragment).
    ``capture_input`` stores a 120-char prefix of the user text the rule read —
    the same text already persisted in ``chat_messages`` — so a firing can be
    traced back to the phrasing that triggered it. Disable globally with
    ``HARNESS_RULE_EVENTS_CAPTURE_INPUT=0``.
    The wrapped function's return value, exceptions and timing are untouched —
    observation happens in a ``finally`` block and cannot alter control flow.
    """

    def decorator(func: Callable) -> Callable:
        if getattr(func, "__harness_observed__", False):
            return func

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            detail: Optional[dict] = None
            started = time.monotonic()
            try:
                result = func(*args, **kwargs)
            except BaseException:
                # Record the attempt, then re-raise untouched.
                record_rule_event(
                    rule_name,
                    None,
                    forced=None,
                    detail={"raised": True, "elapsed_ms": _ms(started)},
                )
                raise
            if extract is not None:
                try:
                    detail = extract(result)
                except Exception:
                    detail = {"extract_failed": True}
            if detail is None:
                detail = {}
            # Generic context that matters for the Phase 3 decision: force rules
            # fire near the iteration cap, so record where the turn was.
            for key in ("iteration", "max_iterations"):
                if key in kwargs and kwargs[key] is not None:
                    detail.setdefault(key, kwargs[key])
            if capture_input:
                prefix = _input_prefix(args, kwargs)
                if prefix:
                    detail.setdefault("input", prefix)
            detail.setdefault("elapsed_ms", _ms(started))
            record_rule_event(rule_name, result, forced=forced, detail=detail)
            return result

        wrapper.__harness_observed__ = True  # type: ignore[attr-defined]
        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        wrapper.__name__ = getattr(func, "__name__", "observed_rule")
        wrapper.__doc__ = getattr(func, "__doc__", None)
        return wrapper

    return decorator


def _input_prefix(args: tuple, kwargs: dict, limit: int = 120) -> Optional[str]:
    """First user-text argument the rule received, clipped. Purely diagnostic."""
    if os.getenv("HARNESS_RULE_EVENTS_CAPTURE_INPUT", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return None
    for candidate in (
        kwargs.get("user_content"),
        kwargs.get("user_message"),
        kwargs.get("text"),
        *args,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate[:limit]
    return None


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


# ── reading the evidence (tests, probes, ops) ────────────────────────────────

def counters() -> dict:
    """Process-local tallies. ``<rule>`` = evaluations, ``<rule>::fired`` = fires."""
    with _LOCK:
        out = dict(_COUNTS)
        out["dropped"] = _DROPPED
        out["buffered"] = len(_BUFFER)
    out["record_failures"] = _RECORD_FAILURES
    out["flush_failures"] = _FLUSH_FAILURES
    return out


def reset_counters() -> None:
    global _DROPPED, _RECORD_FAILURES, _FLUSH_FAILURES
    with _LOCK:
        _COUNTS.clear()
        _BUFFER.clear()
        _DROPPED = 0
    _RECORD_FAILURES = 0
    _FLUSH_FAILURES = 0


def pending_rows() -> list[dict]:
    with _LOCK:
        return list(_BUFFER)


def summary() -> dict:
    """Per-rule evaluations / fires / fire rate — the deletion shortlist."""
    c = counters()
    rules: dict[str, dict] = {}
    for key, value in c.items():
        if key.endswith("::fired"):
            rules.setdefault(key[:-7], {"fired": 0})["fired"] = value
        elif key not in {"dropped", "buffered", "record_failures", "flush_failures"}:
            rules.setdefault(key, {"evaluated": 0, "fired": 0})["evaluated"] = value
    for name, stats in rules.items():
        evaluated = stats.get("evaluated") or stats.get("fired") or 0
        fired = stats.get("fired", 0)
        stats["fire_rate"] = round(fired / evaluated, 4) if evaluated else 0.0
    return {
        "rules": rules,
        "overflow": {"dropped": c.get("dropped", 0), "buffered": c.get("buffered", 0)},
        # Non-zero here means evidence is being LOST — never silently.
        "telemetry": {
            "record_failures": c.get("record_failures", 0),
            "flush_failures": c.get("flush_failures", 0),
        },
    }


# ── persistence (daemon flusher; never on the caller's thread) ───────────────

def _ensure_flusher() -> None:
    global _FLUSHER
    if _FLUSHER is not None and _FLUSHER.is_alive():
        return
    with _LOCK:
        if _FLUSHER is not None and _FLUSHER.is_alive():
            return
        thread = threading.Thread(
            target=_flush_loop, name="harness-rule-events", daemon=True
        )
        _FLUSHER = thread
    thread.start()


def _flush_loop() -> None:
    while True:
        time.sleep(_FLUSH_INTERVAL_S)
        try:
            if not pending_rows():
                continue
            flush_rule_events()
        except Exception:  # pragma: no cover - defensive
            continue


def flush_rule_events(db: Any = None) -> int:
    """Write buffered rows. Returns the number written; never raises.

    ``db`` may be an existing Session (tests pass the fixture session). Without
    one, the app's SessionLocal is used. A failure requeues nothing and is
    counted — losing telemetry is acceptable, breaking a turn is not.
    """
    if not _enabled() or not _persist_enabled():
        return 0
    with _LOCK:
        if not _BUFFER:
            return 0
        rows = list(_BUFFER)
        _BUFFER.clear()
    own_session = db is None
    try:
        if own_session:
            from app.database import SessionLocal
            db = SessionLocal()
        from app.models.harness_rule_event import HarnessRuleEvent

        for row in rows:
            db.add(HarnessRuleEvent(**row))
        db.commit()
        return len(rows)
    except Exception:
        global _FLUSH_FAILURES
        _FLUSH_FAILURES += 1
        logger.debug("harness rule-event flush failed", exc_info=True)
        return 0
    finally:
        if own_session and db is not None:
            try:
                db.close()
            except Exception:  # pragma: no cover
                pass
