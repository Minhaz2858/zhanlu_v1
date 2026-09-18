"""Tool pipeline seam + programmatic tool calling (PTC) tests.

Covers the two halves of the dsh-style pipeline landing:

1. ``execute_tool_batch`` activates one pipeline per batch and every result
   passes the registered post-execute transforms.
2. ``execute_code`` can call the agent's own tools from inside the sandbox, and
   those sub-calls are bounded (recursion, interactivity, count, depth),
   permission-checked, and reported back to the loop.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.agent_loop.tool_executor import execute_tool_batch
from app.services.agent_loop.tool_pipeline import (
    PipelineDenied,
    PipelineState,
    PostTransformResult,
    activate_pipeline,
    active_pipeline,
    clear_post_transforms,
    dispatch_sub_call,
    register_post_transform,
    registered_transform_names,
)
from app.services.tool_handlers.code_execution_tool import _execute_code


class _Decision:
    def __init__(self, allow: bool = True, reason: str = "blocked by test guard"):
        self.allows_execution = allow
        self.reason = reason


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_transforms():
    clear_post_transforms()
    yield
    clear_post_transforms()


# ---------------------------------------------------------------------------
# Pipeline activation
# ---------------------------------------------------------------------------

def test_execute_tool_batch_activates_pipeline():
    seen = {}

    async def _invoke(tool_name, args):
        state = active_pipeline()
        seen["active"] = state is not None
        seen["db"] = getattr(state, "db", None)
        return {"success": True, "tool": tool_name}

    results = _run(
        execute_tool_batch(
            [{"tool_name": "read_file", "args": {"path": "a.txt"}}],
            before_call=lambda n, a: _Decision(True),
            invoke=_invoke,
            db="db-sentinel",
            context={"agent_name": "test-agent"},
        )
    )

    assert seen["active"] is True
    assert seen["db"] == "db-sentinel"
    assert results[0]["tool"] == "read_file"
    # The pipeline must not leak past the batch.
    assert active_pipeline() is None


def test_pipeline_is_not_replaced_by_nested_activation():
    outer = PipelineState(invoke=_noop_invoke, sub_call_limit=5)
    inner = PipelineState(invoke=_noop_invoke, sub_call_limit=99)

    async def _check():
        with activate_pipeline(outer):
            with activate_pipeline(inner) as state:
                assert state is outer, "nested activation must keep the outer guard chain"
            assert outer.depth == 0
            return active_pipeline() is outer

    assert _run(_check()) is True


# ---------------------------------------------------------------------------
# Post-execute transform seam
# ---------------------------------------------------------------------------

def test_post_transforms_accept_replace_and_add_context():
    calls: list[str] = []

    def _accept(tool_name, args, result):
        calls.append("accept")
        return None  # accept as-is

    def _replace(tool_name, args, result):
        calls.append("replace")
        return PostTransformResult(
            result={**result, "tagged": True}, additional_context=["note for the model"]
        )

    register_post_transform(_accept)
    register_post_transform(_replace)
    assert registered_transform_names() == ["_accept", "_replace"]

    async def _invoke(tool_name, args):
        return {"success": True}

    results = _run(
        execute_tool_batch(
            [{"tool_name": "t", "args": {}}],
            before_call=lambda n, a: _Decision(True),
            invoke=_invoke,
        )
    )

    assert calls == ["accept", "replace"]
    assert results[0]["tagged"] is True


def test_raising_post_transform_is_contained():
    def _explode(tool_name, args, result):
        raise RuntimeError("transform bug")

    register_post_transform(_explode)

    async def _invoke(tool_name, args):
        return {"success": True, "value": 1}

    results = _run(
        execute_tool_batch(
            [{"tool_name": "t", "args": {}}],
            before_call=lambda n, a: _Decision(True),
            invoke=_invoke,
        )
    )
    assert results[0] == {"success": True, "value": 1}


# ---------------------------------------------------------------------------
# Sub-call bounds and denials
# ---------------------------------------------------------------------------

def _pipeline_with(invoke, *, before_call=None, limit=25, depth=0) -> PipelineState:
    return PipelineState(
        invoke=invoke, before_call=before_call, sub_call_limit=limit, depth=depth
    )


def test_sub_call_denied_without_pipeline():
    async def _invoke(tool_name, args):  # pragma: no cover - never reached
        return {"success": True}

    with pytest.raises(PipelineDenied) as err:
        _run(dispatch_sub_call("read_file", {}, pipeline=None))
    assert err.value.code == "no_pipeline"


def test_sub_call_blocks_recursion_and_interactive_tools():
    async def _invoke(tool_name, args):  # pragma: no cover - never reached
        return {"success": True}

    state = _pipeline_with(_invoke)
    for tool_name, code in (("execute_code", "recursion_blocked"), ("clarify", "interactive_tool")):
        with pytest.raises(PipelineDenied) as err:
            _run(dispatch_sub_call(tool_name, {}, pipeline=state))
        assert err.value.code == code


def test_sub_call_limit_and_depth_are_enforced():
    async def _invoke(tool_name, args):
        return {"success": True}

    state = _pipeline_with(_invoke, limit=2)
    _run(dispatch_sub_call("read_file", {}, pipeline=state))
    _run(dispatch_sub_call("read_file", {}, pipeline=state))
    with pytest.raises(PipelineDenied) as err:
        _run(dispatch_sub_call("read_file", {}, pipeline=state))
    assert err.value.code == "sub_call_limit"

    deep = _pipeline_with(_invoke, depth=99)
    with pytest.raises(PipelineDenied) as err:
        _run(dispatch_sub_call("read_file", {}, pipeline=deep))
    assert err.value.code == "depth_exceeded"


def test_sub_call_guard_denial_surfaces_reason():
    invoked = {"count": 0}

    async def _invoke(tool_name, args):
        invoked["count"] += 1
        return {"success": True}

    state = _pipeline_with(
        _invoke, before_call=lambda n, a: _Decision(False, "phase lock: build in progress")
    )
    with pytest.raises(PipelineDenied) as err:
        _run(dispatch_sub_call("delete_agent", {}, pipeline=state))
    assert err.value.code == "guard_blocked"
    assert "phase lock" in err.value.reason
    assert invoked["count"] == 0, "guard-denied calls must not reach the tool body"


def test_sub_call_result_is_recorded_on_the_pipeline():
    async def _invoke(tool_name, args):
        return {"success": True, "value": 7}

    state = _pipeline_with(_invoke)
    result = _run(dispatch_sub_call("fake_query", {"q": "x"}, pipeline=state))
    assert result["value"] == 7
    assert state.sub_call_count == 1
    assert state.sub_calls[0]["tool_name"] == "fake_query"


# ---------------------------------------------------------------------------
# PTC in the sandbox (real subprocess round-trip)
# ---------------------------------------------------------------------------

def _ptc_pipeline(invoke, **kwargs):
    return _pipeline_with(invoke, **kwargs)


def test_ptc_round_trip_loops_tools_inside_one_code_block():
    seen_args: list[dict] = []

    async def _invoke(tool_name, args):
        seen_args.append(args)
        return {"success": True, "value": 3, "echo": args.get("region")}

    code = (
        "totals = []\n"
        "for region in ('north', 'south', 'east'):\n"
        "    res = call_tool('fake_query', region=region)\n"
        "    totals.append(res['value'])\n"
        "print('total', sum(totals), 'calls', len(totals), 'ptc', ptc_available)\n"
    )

    state = _ptc_pipeline(_invoke)
    with activate_pipeline(state):
        result = _run(_execute_code({"code": code}, db=None, user_id=None, context=None))

    assert result["success"] is True, result
    assert "total 9 calls 3 ptc True" in result["stdout"]
    assert [a["region"] for a in seen_args] == ["north", "south", "east"]
    assert result["ptc"]["sub_calls"] == 3
    assert result["ptc"]["tools"] == ["fake_query"] * 3


def test_ptc_reports_denial_to_the_code_and_code_can_recover():
    async def _invoke(tool_name, args):  # pragma: no cover - guard denies first
        return {"success": True}

    code = (
        "try:\n"
        "    call_tool('write_file', path='/etc/passwd', content='x')\n"
        "    print('SHOULD-NOT-HAPPEN')\n"
        "except RuntimeError as exc:\n"
        "    print('denied: True', str(exc))\n"
    )

    state = _ptc_pipeline(
        _invoke, before_call=lambda n, a: _Decision(False, "phase lock active")
    )
    with activate_pipeline(state):
        result = _run(_execute_code({"code": code}, db=None, user_id=None, context=None))

    assert result["success"] is True, result
    assert "SHOULD-NOT-HAPPEN" not in result["stdout"]
    assert "denied: True" in result["stdout"]
    # write_file needs the user's confirmation in the default permission mode:
    # the sandbox must be told, not left waiting on an approval nobody can give.
    assert "confirmation" in result["stdout"] or "approve" in result["stdout"]


def test_ptc_guard_denial_reaches_the_code():
    async def _invoke(tool_name, args):  # pragma: no cover - guard denies first
        return {"success": True}

    code = (
        "try:\n"
        "    call_tool('fake_query', region='north')\n"
        "    print('SHOULD-NOT-HAPPEN')\n"
        "except RuntimeError as exc:\n"
        "    print('guard:', 'phase lock active' in str(exc))\n"
    )
    state = _ptc_pipeline(
        _invoke, before_call=lambda n, a: _Decision(False, "phase lock active")
    )
    with activate_pipeline(state):
        result = _run(_execute_code({"code": code}, db=None, user_id=None, context=None))

    assert result["success"] is True, result
    assert "guard: True" in result["stdout"]
    assert result["ptc"]["sub_calls"] == 1
    assert result["ptc"]["call_detail"] == [{"tool": "fake_query", "success": False}]


def test_ptc_is_unavailable_without_an_active_turn():
    code = (
        "print('ptc_available', ptc_available)\n"
        "try:\n"
        "    call_tool('read_file', path='a.txt')\n"
        "except RuntimeError as exc:\n"
        "    print('blocked:', 'not running inside an agent turn' in str(exc))\n"
    )
    result = _run(_execute_code({"code": code}, db=None, user_id=None, context=None))
    assert result["success"] is True, result
    assert "ptc_available False" in result["stdout"]
    assert "blocked: True" in result["stdout"]
    assert result["ptc"]["sub_calls"] == 0


def test_ptc_sandbox_still_blocks_dangerous_imports():
    code = "import os\nprint('escaped', os.getcwd())\n"
    state = _ptc_pipeline(_noop_invoke)
    with activate_pipeline(state):
        result = _run(_execute_code({"code": code}, db=None, user_id=None, context=None))
    assert result["success"] is False
    assert "not allowed" in result["stderr"]


async def _noop_invoke():
    return {"success": True}
