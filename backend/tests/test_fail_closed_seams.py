"""Fail-closed invariants for the tool-execution seams and the compaction wiring.

Every gate in this file already exists. What these tests pin is the DIRECTION of
failure when a gate cannot answer: a policy seam that raises, or a guard that
returns a decision shape nobody understands, must DENY — never silently allow.
The precedent is already in the tree (permission-check exception and
approval-gate exception both deny); the four holes below were the exceptions to
that rule.

Pinned here:

  A. ``pre_tool_use`` hook machinery failure  -> deny (was: log at debug, run anyway)
  B. ``post_tool_use`` declared block         -> result replaced (was: verdict discarded)
  C. guard decision without ``allows_execution`` -> deny (was: default-allow)
     — and an *abstaining* guard (``None``) still allows, unchanged.
  D. post-execute transform failure          -> result withheld (was: accept untouched)
     — unless the transform is registered ``best_effort`` (decorative only).
  E. ``DefaultContextEngine.should_compress`` -> must reach ``should_autocompact``
     at the right arity (was: TypeError, swallowed by the caller at
     routers/agents.py ``_compaction_engine`` -> the pluggable engine silently
     degraded to the inline path on every turn).
  F. ``engine`` is a declared compaction trigger (was: not in the Literal).
  G. the resume loop routes a provider context overflow to compaction (was:
     logged ``should_compress=True``, then answered "Sorry, I encountered an
     error" — the overflow never reached the compactor).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import get_args
from unittest.mock import MagicMock, patch

import pytest

from app.services.agent_loop import tool_pipeline as tp
from app.services.agent_loop.tool_executor import execute_tool_batch
from app.services.agent_loop.tool_pipeline import (
    PipelineDenied,
    PipelineState,
    clear_post_transforms,
    dispatch_sub_call,
    register_post_transform,
)

_AGENTS_PY = Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_transforms():
    clear_post_transforms()
    yield
    clear_post_transforms()


class _OpaqueDecision:
    """A guard decision shape nobody here understands: no ``allows_execution``."""


class _AllowingExecutor:
    """Hook executor stub: every event passes."""

    async def execute(self, event, payload=None):
        from app.services.hooks import HookExecutionResult
        return HookExecutionResult()


class _RaisingExecutor:
    """Hook executor stub that cannot answer (backend down)."""

    async def execute(self, event, payload=None):
        raise RuntimeError("hook backend unavailable")


class _PostBlockingExecutor:
    """Hook executor stub: only POST_TOOL_USE declares a block."""

    async def execute(self, event, payload=None):
        from app.services.hooks import HookExecutionResult
        name = getattr(event, "value", event)
        if name == "post_tool_use":
            return HookExecutionResult(blocked=True, reason="secret material detected")
        return HookExecutionResult()


async def _probe_invoke(tool_name, args):
    return {"success": True, "tool_name": tool_name}


# ---------------------------------------------------------------------------
# A / B — hook seams inside execute_tool
# ---------------------------------------------------------------------------

async def _call_execute_tool(handler=None):
    from app.services.agent_tools import execute_tool

    crud = {"probe_tool": handler or (lambda args, db, uid: {"success": True, "ran": True})}
    with patch("app.services.agent_tools._CRUD_DISPATCH", crud), \
         patch("app.services.tool_registry.ToolRegistry.get_handler", return_value=None):
        return await execute_tool(
            "probe_tool", {}, db=MagicMock(), user_id="u1",
            context={"agent_name": "general_assistant_probe", "conversation_id": "conv-1"},
        )


@pytest.mark.asyncio
async def test_pre_tool_hook_machinery_failure_denies():
    """A pre-tool gate that cannot evaluate is a deny, like the permission gate."""
    with patch("app.services.hooks.get_hook_executor", return_value=_RaisingExecutor()):
        result = await _call_execute_tool()
    assert result.get("success") is False
    assert "fail-closed" in str(result.get("error", "")).lower()


@pytest.mark.asyncio
async def test_pre_tool_hook_success_still_runs_the_tool():
    """Control: a healthy hook executor does not change the path."""
    with patch("app.services.hooks.get_hook_executor", return_value=_AllowingExecutor()):
        result = await _call_execute_tool()
    assert result.get("success") is True
    assert result.get("ran") is True


@pytest.mark.asyncio
async def test_post_tool_hook_declared_block_replaces_the_result():
    """``block_on_failure`` on a post-tool hook must actually block."""
    with patch("app.services.hooks.get_hook_executor", return_value=_PostBlockingExecutor()):
        result = await _call_execute_tool()
    assert result.get("success") is False
    assert "secret material detected" in str(result.get("error", ""))


# ---------------------------------------------------------------------------
# C — guard decision direction (sub-call path + batch path)
# ---------------------------------------------------------------------------

def test_guard_decision_without_allows_execution_denies(monkeypatch):
    monkeypatch.setattr(tp, "_permission_gate", lambda *a, **k: None)
    state = PipelineState(invoke=_probe_invoke, before_call=lambda n, a: _OpaqueDecision())
    with pytest.raises(PipelineDenied) as ei:
        _run(dispatch_sub_call("probe_tool", {}, pipeline=state))
    assert ei.value.code == "guard_blocked"


def test_abstaining_guard_still_allows(monkeypatch):
    """``None`` from a guard means "abstain" — not a deny. Unchanged behaviour."""
    monkeypatch.setattr(tp, "_permission_gate", lambda *a, **k: None)
    state = PipelineState(invoke=_probe_invoke, before_call=lambda n, a: None)
    result = _run(dispatch_sub_call("probe_tool", {}, pipeline=state))
    assert result["success"] is True


def test_batch_guard_decision_without_allows_execution_blocks_call():
    """The batch path must normalize an unknown decision, not raise AttributeError."""
    calls = [{"tool_name": "probe_tool", "args": {}, "tool_call_id": "c1"}]
    results = _run(execute_tool_batch(
        calls,
        before_call=lambda n, a: _OpaqueDecision(),
        invoke=_probe_invoke,
    ))
    assert len(results) == 1
    assert results[0]["success"] is False


# ---------------------------------------------------------------------------
# D — post-execute transform direction
# ---------------------------------------------------------------------------

def _raising_transform(tool_name, args, result):
    raise RuntimeError("result policy engine down")


def test_post_transform_failure_withholds_the_result():
    register_post_transform(_raising_transform, name="policy_redact")
    out, _contexts = tp.apply_post_transforms(
        "probe_tool", {}, {"success": True, "data": "secret"}
    )
    assert out["success"] is False
    assert "policy_redact" in str(out.get("error", ""))


def test_best_effort_transform_failure_keeps_the_result():
    register_post_transform(_raising_transform, name="decorative", best_effort=True)
    out, _contexts = tp.apply_post_transforms(
        "probe_tool", {}, {"success": True, "data": "secret"}
    )
    assert out == {"success": True, "data": "secret"}


def test_healthy_transform_still_replaces_the_result():
    register_post_transform(lambda t, a, r: {"success": True, "trimmed": True}, name="trim")
    out, _contexts = tp.apply_post_transforms("probe_tool", {}, {"success": True})
    assert out == {"success": True, "trimmed": True}


# ---------------------------------------------------------------------------
# E / F — compaction: engine arity + trigger vocabulary
# ---------------------------------------------------------------------------

def test_context_engine_should_compress_reaches_should_autocompact():
    """Unpatched call: the engine must not raise on its own seam."""
    from app.services.context_engine import DefaultContextEngine

    engine = DefaultContextEngine(model="deepseek-chat")
    assert engine.should_compress([{"role": "user", "content": "hi"}]) is False


def test_context_engine_should_compress_true_over_threshold():
    from app.services.context_engine import DefaultContextEngine

    engine = DefaultContextEngine(model="deepseek-chat")
    huge = [{"role": "user", "content": "x" * 4000} for _ in range(400)]
    assert engine.should_compress(huge) is True


def test_compact_trigger_declares_the_engine_caller():
    from app.services.compaction import CompactTrigger

    assert "engine" in get_args(CompactTrigger)


# ---------------------------------------------------------------------------
# G — resume loop routes context overflow to compaction
# ---------------------------------------------------------------------------

def test_resume_loop_routes_context_overflow_to_compaction():
    """Wiring pin.

    ``resume_conversation`` is a router function inside a ~19k-line module and
    cannot be exercised in isolation, so this asserts the wiring the same way the
    other loop does it: the classify -> should_compress -> auto_compact_if_needed
    chain must appear inside the resume error handler.
    """
    src = _AGENTS_PY.read_text(encoding="utf-8")
    marker = "LLM call error in conversation %s (resume)"
    idx = src.index(marker)
    handler = src[idx: idx + 4000]
    assert "ce.should_compress" in handler, "resume handler must inspect overflow signal"
    assert "auto_compact_if_needed" in handler, "resume handler must attempt compaction"
    assert "trigger=\"reactive\"" in handler, "compaction must be tagged reactive"
