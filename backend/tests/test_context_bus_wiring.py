"""Tests for ContextBus wiring into both agent paths (IBM context convergence).

1. ExecutionRequest accepts a context_bus and the FSM's CONTEXT stage merges
   bus sections into the assembled manifest (one context model, two brains).
2. The v3 stream router creates the bus before the FSM/ReAct branch, passes
   it to ExecutionRequest, injects the ReAct block, and drops it at every
   terminal path (source-level guards — the full stream needs a live LLM).
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.context_bus import ContextBus, get_context_bus, drop_context_bus


# ---------------------------------------------------------------- FSM merge

def _fsm_with_db():
    """A SynexiaFSM instance with mocked db/execution — only _run_context runs."""
    from app.services.synexia.fsm import SynexiaFSM

    fsm = object.__new__(SynexiaFSM)
    fsm.db = MagicMock()
    fsm.execution = MagicMock()
    fsm.execution.task_spec = {"task_kind": "answer_question"}
    return fsm


def _patched_assemble(items=None):
    def fake_assemble(**kwargs):
        return {"items": list(items or []), "token_estimate": 10,
                "available_tools": ["tool_a"], "available_skills": [],
                "user_attachments": []}
    return fake_assemble


def test_execution_request_accepts_context_bus():
    from app.services.synexia.fsm import ExecutionRequest

    bus = ContextBus("c1")
    req = ExecutionRequest(user_message="hi", context_bus=bus)
    assert req.context_bus is bus


def test_run_context_merges_bus_manifest():
    from app.services.synexia.fsm import ExecutionRequest

    fsm = _fsm_with_db()
    bus = ContextBus("c1")
    bus.publish("system", "analyst", {"tools": ["sql", "files"], "skills": ["etl"]})
    bus.publish("task_state", "plan: count lots")
    bus.publish("available_tools_shadow", "ignored")  # unknown keys still become items

    req = ExecutionRequest(user_message="hi", context_bus=bus)
    with patch("app.services.synexia.context_assembler.assemble_context",
               _patched_assemble()):
        fsm._run_context(req)

    manifest = fsm.execution.context_manifest
    types = [i["type"] for i in manifest["items"]]
    assert "system" in types and "task_state" in types
    # union of available_tools (assembler's + bus's)
    assert set(manifest["available_tools"]) >= {"tool_a", "sql", "files"}
    assert manifest["available_skills"] == ["etl"]


def test_run_context_without_bus_unchanged():
    from app.services.synexia.fsm import ExecutionRequest

    fsm = _fsm_with_db()
    req = ExecutionRequest(user_message="hi")
    with patch("app.services.synexia.context_assembler.assemble_context",
               _patched_assemble()):
        fsm._run_context(req)
    manifest = fsm.execution.context_manifest
    assert manifest["token_estimate"] == 10
    assert manifest["available_tools"] == ["tool_a"]


def test_run_context_bus_failure_is_non_fatal():
    from app.services.synexia.fsm import ExecutionRequest

    fsm = _fsm_with_db()
    bus = MagicMock()
    bus.to_fsm_manifest.side_effect = RuntimeError("bus exploded")
    req = ExecutionRequest(user_message="hi", context_bus=bus)
    with patch("app.services.synexia.context_assembler.assemble_context",
               _patched_assemble()):
        fsm._run_context(req)  # must not raise
    manifest = fsm.execution.context_manifest
    assert manifest["token_estimate"] == 10


# ---------------------------------------------------------------- router wiring

def _agents_source():
    import inspect
    from app.routers import agents
    return inspect.getsource(agents)


def test_v3_router_creates_bus_before_branch():
    src = _agents_source()
    # bus init sits with _v3_conv_ctx, BEFORE the FSM trigger block
    assert "get_context_bus(conversation_id)" in src
    conv_idx = src.find("_v3_conv_ctx = None")
    bus_idx = src.find("get_context_bus(conversation_id)")
    fsm_idx = src.find("_v3_plan_trigger = PlanTrigger(")
    assert -1 not in (conv_idx, bus_idx, fsm_idx)
    assert conv_idx < bus_idx < fsm_idx


def test_v3_router_passes_bus_to_fsm_and_react():
    src = _agents_source()
    assert "context_bus=_bus," in src          # ExecutionRequest field
    assert 'skip={"transcript", "artifacts", "memory"}' in src  # ReAct injection


def test_v3_router_drops_bus_on_all_terminal_paths():
    src = _agents_source()
    # FSM done, workflow done, cache-hit done, ReAct finally = 4 sites
    assert src.count("drop_context_bus(conversation_id)") >= 4


def test_tool_digest_published_in_react_loop():
    src = _agents_source()
    assert "digest_tool_result(tool_name, _digest_payload)" in src
