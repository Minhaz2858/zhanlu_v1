"""Per-turn SQL execution budget (2026-10-29 verification-gate closure).

Why: the 483s sales-report incident ran 9 SQL nodes / 6 ReAct
``query_database`` calls in ONE user turn. The FSM plan cap
(``NL2SQL_MAX_REPORT_NODES``) bounds plan-shaped turns, but the ReAct
tool-loop path is bounded only by generic iteration caps — so a planning
regression on ANY path can re-create the latency. This module bounds the
number of warehouse SQL executions per TURN at the one boundary every
NL2SQL path shares: ``NLAnswerService.answer``.

Design constraints:
- Process-local registry (dict of deques). A turn's tool calls all execute
  in one backend process, so a process-local counter bounds exactly the
  runaway-loop case. It is honest about not being a cluster-wide quota —
  that would need shared state and is a different problem.
- Scope resolution is explicit: an explicit ``turn_id`` (threaded from the
  chat loop — one user message = one turn) wins; otherwise an
  ``execution_id`` (one FSM run); otherwise ``conversation_id`` with a
  time window. No scope → no budget (single-shot API paths are bounded by
  their own one-call shape).
- Refusals are honest and actionable: the caller is told to synthesize
  from the results already gathered, not silently truncated.
"""
from __future__ import annotations

import threading
import time
from collections import deque

_LOCK = threading.Lock()
_EXECUTIONS: dict[str, deque[float]] = {}

#: Retained memory bound: a scope older than this never counts, so drop it.
_MAX_WINDOW_S = 24 * 3600


def resolve_scope(
    turn_id: str | None = None,
    execution_id: str | None = None,
    conversation_id: str | None = None,
) -> str | None:
    """Turn-scoped key, most specific wins. None → budget not applicable."""
    if turn_id:
        return f"turn:{turn_id}"
    if execution_id:
        return f"exec:{execution_id}"
    if conversation_id:
        return f"conv:{conversation_id}"
    return None


def _purge(dq: deque[float], window_s: float) -> None:
    cutoff = time.monotonic() - max(window_s, 1.0)
    while dq and dq[0] < cutoff:
        dq.popleft()


def count_recent(scope: str, window_s: float = _MAX_WINDOW_S) -> int:
    with _LOCK:
        dq = _EXECUTIONS.get(scope)
        if dq is None:
            return 0
        _purge(dq, window_s)
        return len(dq)


def register_execution(scope: str, window_s: float = _MAX_WINDOW_S) -> None:
    """Record one real warehouse execution (called AFTER it succeeded)."""
    if not scope:
        return
    with _LOCK:
        dq = _EXECUTIONS.setdefault(scope, deque())
        _purge(dq, window_s)
        dq.append(time.monotonic())
        # Hard memory bound per scope: a turn can never legitimately need
        # thousands of entries; keep the deque small.
        while len(dq) > 512:
            dq.popleft()


def budget_check(
    scope: str | None,
    max_executions: int,
    window_s: float = _MAX_WINDOW_S,
) -> tuple[bool, int]:
    """(allowed, already_used). max_executions <= 0 → unlimited (off)."""
    if not scope or max_executions <= 0:
        return True, 0
    used = count_recent(scope, window_s)
    return used < max_executions, used


def effective_budget(base_max: int, scope: str | None) -> int:
    """Budget for a scope. Explicit turn/execution scopes enforce the
    configured value as-is; a conversation-scoped fallback (caller threaded
    no turn id) gets 2x within its window so consecutive normal turns do
    not trip the guard while the runaway shape stays bounded."""
    if not scope:
        return base_max
    if scope.startswith("conv:"):
        return max(base_max, base_max * 2)
    return base_max


def budget_verdict(
    scope: str | None,
    base_max: int,
    window_s: float,
) -> tuple[bool, int, int]:
    """(allowed, used, effective_max) — shared by every enforcement site
    (NLAnswerService.answer, db_tools._execute_query) so they cannot drift."""
    eff = effective_budget(base_max, scope)
    allowed, used = budget_check(scope, eff, window_s)
    return allowed, used, eff


def has_capacity(
    scope: str | None,
    base_max: int,
    window_s: float,
) -> bool:
    """READ-ONLY availability probe: True when at least one attempt could
    still be acquired. Reserves NOTHING — :func:`try_acquire` inside SQL
    execution remains the atomic source of truth. Used for pre-flight
    rejection of tool invocations whose full FSM startup + planner cost
    would be wasted on a guaranteed refusal (measured 2026-10-30: one
    refused attempt cost 159s of avoidable orchestration). A race between
    this probe and a concurrent acquire is benign: the worst case is a
    refused attempt that reached the inner check, never over-execution."""
    if not scope or base_max <= 0:
        return True
    return count_recent(scope, window_s) < effective_budget(base_max, scope)


def refusal_payload(
    used: int,
    max_executions: int,
    source_id: str | None = None,
) -> dict:
    """The canonical budget-refusal result shape, shared by every refusal
    site so the caller-side LLM guidance cannot drift."""
    payload = {
        "success": False,
        "error_kind": "turn_sql_budget_exhausted",
        "error": (
            f"This turn has already spent {used} of {max_executions} allowed SQL "
            "attempts against the data source. Do not run further queries in this "
            "turn — answer from the results already gathered and clearly disclose "
            "anything that was not queried. If more data is essential, ask the "
            "user to send a follow-up message."
        ),
        "turn_sql_budget": {"max": max_executions, "used": used},
    }
    if source_id:
        payload["source_id"] = source_id
    return payload


def try_acquire(
    scope: str | None,
    base_max: int,
    window_s: float,
) -> tuple[bool, int, int]:
    """Atomically reserve ONE SQL attempt: (acquired, used_after, effective_max).

    Charges the attempt UPFRONT — validation failures, gate rejections,
    execution failures and repair re-executions are all real attempts the
    warehouse (or the pipeline) paid for, so they count exactly like
    successes. The check-and-increment is one locked critical section, so
    concurrent tool calls in the same turn cannot both slip in at
    budget-1.

    Scope is process-local BY DESIGN: a turn's tool calls execute in one
    backend process. This is NOT a cluster-wide quota — do not represent
    it as one.
    """
    if not scope or base_max <= 0:
        return True, 0, max(0, base_max)
    eff = effective_budget(base_max, scope)
    now = time.monotonic()
    with _LOCK:
        dq = _EXECUTIONS.setdefault(scope, deque())
        _purge(dq, window_s)
        if len(dq) >= eff:
            return False, len(dq), eff
        dq.append(now)
        while len(dq) > 512:
            dq.popleft()
        return True, len(dq), eff


def reset_scope(scope: str) -> None:
    """Test/operational helper: clear one scope's history."""
    with _LOCK:
        _EXECUTIONS.pop(scope, None)
