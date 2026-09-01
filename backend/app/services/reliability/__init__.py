"""Tool-call reliability layer.

Wraps every tool invocation with four ordered safety nets so a transient
or recoverable failure surfaces a *useful* error rather than silently
blowing up the agent's plan.

  1. **Retry with backoff** (retry_with_backoff)
     Up to N attempts with exponential backoff. Idempotent-friendly tools
     benefit; state-changing tools short-circuit the retry on partial
     success indicators.

  2. **Argument reformulation** (reformulate_args)
     When the same call fails twice, ask the LLM to repair the args
     (typos in field names, unit mismatches, missing required keys,
     etc.) and re-run. Capped to one reformulation per call so a broken
     contract doesn't loop forever.

  3. **Output verification** (verify_output)
     Inspect the tool result against the tool's declared output schema
     (or a tiny whitelist of invariants — e.g. "is dict" / "has
     artifact_id" / "mime_type matches file_type") and surface a
     structured error when invariants fail.

  4. **Smarter loop guard** (loop_guard_v2)
     Track ``(tool_name, args_hash, success)`` triples and abort only
     when the SAME call has succeeded AND re-issued — i.e. a real loop.
     A real call that fails N times in a row is a *bug*, not a loop, and
     is surfaced to the planner for replan (not silently dropped).

The legacy ``_detect_tool_call_loop`` in ``routers/agents.py`` keys only
on cardinality; the v2 guard fixes the false-positive problem where
legitimate retries get treated as runaway loops.

All four pieces are exposed as small standalone functions so they can be
unit-tested without spinning up the full FSM.  ``run_tool_with_reliability``
is the single entry point a caller should use.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────
# All knobs are read at call time so tests can monkeypatch them; in
# production they're driven by config.py via get_reliability_config().
@dataclass
class ReliabilityConfig:
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_factor: float = 2.0
    backoff_jitter: float = 0.1
    max_reformulations: int = 1
    loop_guard_threshold: int = 3
    loop_guard_window: int = 10  # look at the last N calls

    @classmethod
    def from_settings(cls) -> "ReliabilityConfig":
        try:
            from app.config import settings

            return cls(
                max_retries=int(getattr(settings, "TOOL_RELIABILITY_MAX_RETRIES", 2)),
                backoff_base_seconds=float(
                    getattr(settings, "TOOL_RELIABILITY_BACKOFF_BASE", 0.5)
                ),
                backoff_factor=float(getattr(settings, "TOOL_RELIABILITY_BACKOFF_FACTOR", 2.0)),
                backoff_jitter=float(getattr(settings, "TOOL_RELIABILITY_BACKOFF_JITTER", 0.1)),
                max_reformulations=int(getattr(settings, "TOOL_RELIABILITY_MAX_REFORMULATIONS", 1)),
                loop_guard_threshold=int(getattr(settings, "TOOL_RELIABILITY_LOOP_GUARD_THRESHOLD", 3)),
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("reliability: could not read settings (%s); using defaults", exc)
            return cls()


# ── Loop guard v2 ────────────────────────────────────────────────────────
@dataclass
class LoopState:
    """Per-conversation rolling window of recent tool calls."""

    history: list[tuple[str, str, bool]] = field(default_factory=list)
    # Each entry: (tool_name, args_hash, success)

    def record(self, tool_name: str, args: dict, success: bool) -> None:
        self.history.append((tool_name, _hash_args(args), success))
        # Bound the history; the loop guard looks at most LOOP_GUARD_WINDOW.
        if len(self.history) > 64:
            self.history = self.history[-64:]

    def is_loop(self, cfg: ReliabilityConfig) -> tuple[bool, str]:
        """Return (is_loop, reason).

        A "loop" is defined as: the same tool called with the same args
        has succeeded and been re-issued, *at least* ``threshold`` times
        in the last ``window`` calls.  Failures don't count toward the
        loop signal (they're a real bug to surface, not a loop to
        suppress).
        """
        if len(self.history) < cfg.loop_guard_threshold:
            return False, ""
        window = self.history[-cfg.loop_guard_window :]
        # Count (tool, args) pairs that succeeded at least once.
        # A loop is "same call succeeded multiple times" — not the same
        # call failing repeatedly (which is a different bug class).
        from collections import Counter
        success_keys = [
            f"{t}|{a}" for (t, a, ok) in window if ok
        ]
        counts = Counter(success_keys)
        if not counts:
            return False, ""
        most_common_key, most_common_count = counts.most_common(1)[0]
        if most_common_count >= cfg.loop_guard_threshold:
            return True, (
                f"Loop detected: tool={most_common_key.split('|', 1)[0]} "
                f"re-issued {most_common_count}× with identical args"
            )
        return False, ""


def _hash_args(args: dict) -> str:
    """Stable, JSON-canonical hash of tool args (order-independent)."""
    try:
        canonical = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ── Per-conversation LoopState (contextvar) ──────────────────────────────
# Mirrors the _CURRENT_EXECUTION_ID pattern in automation_executor.py: set
# once per automation run (or chat conversation) so every tool call in that
# run shares one LoopState for loop-guard history. When unset (e.g. a call
# outside a conversation), _execute_with_reliability creates a transient
# LoopState — the loop guard then has no cross-call history but still works.
import contextvars

_CONVERSATION_LOOP_STATE: contextvars.ContextVar["LoopState | None"] = contextvars.ContextVar(
    "conversation_loop_state", default=None,
)


def get_conversation_loop_state() -> "LoopState | None":
    """Return the LoopState for the current conversation/run, or None."""
    return _CONVERSATION_LOOP_STATE.get()


def set_conversation_loop_state(ls: "LoopState | None"):
    """Set the LoopState for the current context; returns a reset token."""
    return _CONVERSATION_LOOP_STATE.set(ls)


def reset_conversation_loop_state(token) -> None:
    """Reset the LoopState contextvar using a token from set_conversation_loop_state."""
    _CONVERSATION_LOOP_STATE.reset(token)


# ── Retry with backoff ───────────────────────────────────────────────────
async def retry_with_backoff(
    fn: Callable[[], Awaitable[Any]],
    *,
    cfg: ReliabilityConfig,
    tool_name: str = "<unknown>",
    is_retryable: Optional[Callable[[Any], bool]] = None,
) -> tuple[Any, int]:
    """Run ``fn`` with exponential backoff. Returns (result, attempts).

    Args:
        fn:             Async callable that performs the tool call.
        cfg:            Reliability config.
        tool_name:      Name for logging.
        is_retryable:   Optional predicate(result_or_exc) → bool. When
                        provided, non-retryable errors short-circuit the
                        retry. When omitted, every exception is retried.

    Returns:
        (result, attempts) where ``attempts`` is the number of attempts
        actually made (1-based). On the final attempt, the exception
        propagates to the caller.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, cfg.max_retries + 1):
        try:
            result = await fn()
            if attempt > 1:
                logger.info(
                    "reliability: tool %s succeeded on attempt %d/%d",
                    tool_name, attempt, cfg.max_retries,
                )
            return result, attempt
        except Exception as exc:
            last_exc = exc
            if is_retryable is not None and not is_retryable(exc):
                logger.debug(
                    "reliability: tool %s non-retryable error: %s",
                    tool_name, exc,
                )
                raise
            if attempt >= cfg.max_retries:
                logger.warning(
                    "reliability: tool %s exhausted %d attempts; last error: %s",
                    tool_name, cfg.max_retries, exc,
                )
                raise
            sleep_seconds = cfg.backoff_base_seconds * (
                cfg.backoff_factor ** (attempt - 1)
            ) + cfg.backoff_jitter * (attempt - 1)
            logger.info(
                "reliability: tool %s attempt %d/%d failed (%s); sleeping %.2fs",
                tool_name, attempt, cfg.max_retries, exc, sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)
    # Unreachable but keep type-checkers happy.
    assert last_exc is not None
    raise last_exc


# ── Argument reformulation ──────────────────────────────────────────────
async def reformulate_args(
    *,
    tool_name: str,
    args: dict,
    error: BaseException,
    cfg: ReliabilityConfig,
    llm_repair: Optional[Callable[[str, dict, str], Awaitable[Optional[dict]]]] = None,
) -> Optional[dict]:
    """Ask the LLM to repair a failing tool call's args.

    The caller supplies an ``llm_repair(tool_name, args, error_str)`` hook
    that returns a new args dict, or ``None`` to give up.  When
    ``llm_repair`` is ``None``, the function is a no-op (the caller is
    responsible for the decision).

    Returns the new args dict, or ``None`` when no repair is available.
    """
    if llm_repair is None:
        return None
    if cfg.max_reformulations <= 0:
        return None
    try:
        repaired = await llm_repair(tool_name, args, repr(error))
    except Exception as exc:
        logger.warning(
            "reliability: llm_repair for %s raised (non-fatal): %s",
            tool_name, exc,
        )
        return None
    if not isinstance(repaired, dict) or repaired == args:
        return None
    logger.info(
        "reliability: reformulated args for tool %s (key diff: %s)",
        tool_name,
        _diff_keys(args, repaired),
    )
    return repaired


def _diff_keys(a: dict, b: dict) -> list[str]:
    keys = set(a.keys()) | set(b.keys())
    return [k for k in keys if a.get(k) != b.get(k)]


# ── Output verification ─────────────────────────────────────────────────
@dataclass
class OutputCheck:
    """Single invariant on a tool result."""

    name: str
    predicate: Callable[[Any], bool]
    error: str


def default_output_checks(tool_name: str) -> list[OutputCheck]:
    """Return a sensible default set of checks for known artifact tools."""
    if tool_name in ("create_artifact", "run_sandbox_skill"):
        return [
            OutputCheck(
                name="is_dict",
                predicate=lambda r: isinstance(r, dict),
                error="tool result is not a dict",
            ),
            OutputCheck(
                name="has_artifact_id",
                predicate=lambda r: bool(r.get("artifact_id") or r.get("id")),
                error="artifact tool returned no artifact_id/id",
            ),
        ]
    return []


def verify_output(result: Any, checks: list[OutputCheck]) -> tuple[bool, str]:
    """Run each check; return (ok, error_message).

    On the first failure, ``ok`` is ``False`` and ``error_message`` is the
    check's error string.  When all checks pass, returns ``(True, "")``.
    """
    for check in checks:
        try:
            ok = check.predicate(result)
        except Exception as exc:
            return False, f"check {check.name!r} raised: {exc}"
        if not ok:
            return False, check.error
    return True, ""


# ── Single entry point ──────────────────────────────────────────────────
async def run_tool_with_reliability(
    tool_name: str,
    args: dict,
    *,
    call_fn: Callable[[dict], Awaitable[Any]],
    loop_state: LoopState,
    cfg: Optional[ReliabilityConfig] = None,
    output_checks: Optional[list[OutputCheck]] = None,
    llm_repair: Optional[Callable[[str, dict, str], Awaitable[Optional[dict]]]] = None,
    is_retryable: Optional[Callable[[Any], bool]] = None,
) -> dict:
    """Run a tool with the full reliability stack.

    Args:
        tool_name:      Display name for logs / loop guard.
        args:           Initial argument dict.
        call_fn:        Async ``(args) -> result`` that performs the call.
        loop_state:     Shared per-conversation ``LoopState``.
        cfg:            Reliability config; defaults read from settings.
        output_checks:  Optional list of output invariants.
        llm_repair:     Optional LLM-based arg repair hook.
        is_retryable:   Optional exception classifier.

    Returns:
        A dict shaped like ``{"success": bool, "result"?: Any, "error"?: str,
        "attempts": int, "reformulated": bool}``.  Never raises — failures
        are encoded in the dict so the planner can decide what to do.
    """
    cfg = cfg or ReliabilityConfig.from_settings()
    current_args = args
    reformulated = False
    attempts = 0

    try:
        # 1. retry → reformulate → verify, in that order.
        result, attempts = await retry_with_backoff(
            lambda: call_fn(current_args),
            cfg=cfg,
            tool_name=tool_name,
            is_retryable=is_retryable,
        )
    except Exception as exc:
        # Even if every retry failed, count what we actually tried.
        attempts = getattr(exc, "_attempts_made", attempts) or cfg.max_retries
        # One shot at argument reformulation, then one more retry.
        if llm_repair is not None and cfg.max_reformulations > 0:
            new_args = await reformulate_args(
                tool_name=tool_name,
                args=current_args,
                error=exc,
                cfg=cfg,
                llm_repair=llm_repair,
            )
            if new_args is not None:
                reformulated = True
                current_args = new_args
                try:
                    result, attempts = await retry_with_backoff(
                        lambda: call_fn(current_args),
                        cfg=cfg,
                        tool_name=tool_name,
                        is_retryable=is_retryable,
                    )
                except Exception as exc2:
                    return {
                        "success": False,
                        "error": str(exc2),
                        "attempts": cfg.max_retries + attempts,
                        "reformulated": reformulated,
                    }
            else:
                return {
                    "success": False,
                    "error": str(exc),
                    "attempts": attempts or cfg.max_retries,
                    "reformulated": reformulated,
                }
        else:
            return {
                "success": False,
                "error": str(exc),
                "attempts": attempts or cfg.max_retries,
                "reformulated": reformulated,
            }

    # 2. Output verification.
    if output_checks:
        ok, err = verify_output(result, output_checks)
        if not ok:
            return {
                "success": False,
                "error": f"output verification failed: {err}",
                "result": result,
                "attempts": attempts,
                "reformulated": reformulated,
            }

    # 3. Loop guard: record this successful call and check the rolling
    #    window.  We check AFTER success so a successful call that
    #    *follows* a series of failures doesn't accidentally look like
    #    a fresh loop.
    loop_state.record(tool_name, current_args, success=True)
    is_loop, reason = loop_state.is_loop(cfg)
    if is_loop:
        return {
            "success": False,
            "error": f"loop guard: {reason}",
            "result": result,
            "attempts": attempts,
            "reformulated": reformulated,
        }

    return {
        "success": True,
        "result": result,
        "attempts": attempts,
        "reformulated": reformulated,
    }


__all__ = [
    "ReliabilityConfig",
    "LoopState",
    "OutputCheck",
    "default_output_checks",
    "retry_with_backoff",
    "reformulate_args",
    "verify_output",
    "run_tool_with_reliability",
]
