"""Span factories + context managers for the Zhanlu agent stack.

Every helper is best-effort: a tracing failure must never break an LLM call,
tool execution, FSM run, or workflow node. Span creation honors
``TRACE_SPAN_SAMPLING_RATE`` (1.0 = record everything); metrics recording
honors ``TRACE_METRICS_ENABLED``.
"""
from __future__ import annotations

import contextvars
import functools
import random
import sys
import time
from contextlib import contextmanager
from typing import Any, AsyncIterator, Callable, Iterator

from app.config import settings
from app.services.tracing import get_tracer
from app.services.tracing import genai
from app.services.tracing import metrics
from app.services.tracing.base import Span

_active_llm_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "active_llm_span", default=None,
)


def _sampled() -> bool:
    try:
        rate = float(getattr(settings, "TRACE_SPAN_SAMPLING_RATE", 1.0))
    except (TypeError, ValueError):
        rate = 1.0
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


def _make_span(name: str, attrs: dict[str, Any] | None = None) -> Span:
    if _sampled():
        return get_tracer().start_span(name, attrs)
    return Span(name, attrs)


def _elapsed_ms(span: Span | None) -> float:
    if span is None:
        return 0.0
    try:
        return (time.monotonic() - span._start) * 1000  # noqa: SLF001
    except Exception:
        return 0.0


# ── LLM spans ───────────────────────────────────────────────────────────

def llm_call_span(model: str, temperature: float | None = None) -> Span:
    """A span for one LLM completion (created lazily, closed by caller)."""
    return _make_span("zhanlu.chat", genai.llm_attributes(model, temperature))


def note_llm_usage(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    error: str | None = None,
) -> None:
    """Attach usage/cost attributes to the active LLM span + metrics.

    Call inside an ``llm_completion_span`` / ``traced_llm_call`` scope at the
    point where token usage is known (mirrors ``record_llm_call`` call sites).
    """
    span = _active_llm_span.get()
    attrs = genai.usage_attributes(prompt_tokens, completion_tokens)
    attrs[genai.GEN_AI_REQUEST_MODEL] = model
    if error:
        attrs["error"] = error
    if span is not None:
        for key, value in attrs.items():
            try:
                span.set_attribute(key, value)
            except Exception:
                pass
    metrics.get_metrics().record_llm(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=_elapsed_ms(span),
        error=error,
    )


@contextmanager
def llm_completion_span(
    model: str, temperature: float | None = None,
) -> Iterator[Span]:
    """Context manager wrapping one LLM completion scope.

    Records the ``error`` attribute when an exception escapes so callers
    with early returns / raises still produce complete spans.
    """
    span = llm_call_span(model, temperature=temperature)
    token = _active_llm_span.set(span)
    span.__enter__()
    try:
        yield span
    except BaseException:
        span.set_attribute("error", genai.error_text(sys.exc_info()[1]))
        raise
    finally:
        _active_llm_span.reset(token)
        span.__exit__(None, None, None)


def traced_llm_call() -> Callable:
    """Decorator wrapping an async LLM function in an active LLM span.

    The span starts before the function body (model resolved inside) and is
    enriched by ``note_llm_usage`` at the usage-recording sites. Safe with
    early returns and raises.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            span = llm_call_span("")
            token = _active_llm_span.set(span)
            span.__enter__()
            try:
                return await fn(*args, **kwargs)
            except BaseException:
                span.set_attribute("error", genai.error_text(sys.exc_info()[1]))
                raise
            finally:
                _active_llm_span.reset(token)
                span.__exit__(None, None, None)

        return wrapper

    return decorator


# ── Tool spans ──────────────────────────────────────────────────────────

def tool_call_span(tool_name: str, arguments: dict | None = None) -> Span:
    """A span for one tool invocation (attributes incl. args hash/preview)."""
    return _make_span(f"tool.{tool_name}", genai.tool_attributes(tool_name, arguments))


@contextmanager
def tool_exec_span(tool_name: str, arguments: dict | None = None) -> Iterator[Span]:
    """Context manager around actual tool dispatch.

    Caller sets ``gen_ai.tool.success`` / ``error`` attributes inside; on exit
    the rollup records duration + success/error for the Admin dashboard.
    """
    span = tool_call_span(tool_name, arguments)
    span.__enter__()
    try:
        yield span
    finally:
        success = bool(span.attributes.get(genai.GEN_AI_TOOL_SUCCESS, False))
        error = span.attributes.get("error")
        metrics.get_metrics().record_tool(
            tool_name=tool_name,
            duration_ms=_elapsed_ms(span),
            success=success,
            error=error,
        )
        span.__exit__(None, None, None)


# ── FSM spans ───────────────────────────────────────────────────────────

@contextmanager
def fsm_run_span(
    execution_id: str, agent_name: str | None, mode: str | None,
) -> Iterator[Span]:
    """Span covering one Synexia FSM execution (INIT→DONE/FAIL)."""
    span = _make_span("fsm.run", {
        "execution.id": execution_id,
        "agent_name": agent_name or "",
        "execution.mode": mode or "",
    })
    span.__enter__()
    try:
        yield span
    except BaseException:
        span.set_attribute("error", genai.error_text(sys.exc_info()[1]))
        raise
    finally:
        metrics.get_metrics().record_fsm_run(error=span.attributes.get("error"))
        span.__exit__(None, None, None)


@contextmanager
def fsm_state_span(state: str, execution_id: str | None) -> Iterator[Span]:
    """Span + rollup entry for one FSM state transition."""
    metrics.get_metrics().record_fsm_transition(state)
    span = _make_span(f"fsm.state.{state}", {
        "execution.id": execution_id or "",
        "fsm.state": state,
    })
    span.__enter__()
    try:
        yield span
    finally:
        span.__exit__(None, None, None)


# ── Workflow-node spans ─────────────────────────────────────────────────

@contextmanager
def workflow_node_span(node_id: str, node_type: str) -> Iterator[Span]:
    """Span around one workflow-graph node execution."""
    span = _make_span(f"workflow.node.{node_type}", {
        "workflow.node.id": node_id,
        "workflow.node.type": node_type,
    })
    span.__enter__()
    try:
        yield span
    finally:
        metrics.get_metrics().record_workflow_node(
            node_type=node_type,
            duration_ms=_elapsed_ms(span),
            success=bool(span.attributes.get("success", True)),
        )
        span.__exit__(None, None, None)


__all__ = [
    "llm_call_span",
    "llm_completion_span",
    "note_llm_usage",
    "traced_llm_call",
    "tool_call_span",
    "tool_exec_span",
    "fsm_run_span",
    "fsm_state_span",
    "workflow_node_span",
]
