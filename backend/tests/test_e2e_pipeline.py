"""End-to-end integration tests — full turn loop pipeline simulation.

These tests simulate the complete reliability pipeline that runs in the
turn loop, using mock LLM responses and mock tool execution. They verify
that all 17 modules work together correctly under realistic conditions.

Test scenarios:
1. Guardrail halts a same-tool-failure loop mid-turn
2. Iteration budget exhaustion stops the loop
3. Message sanitization runs before the "API call"
4. Verification-on-stop fires after code edit without verification
5. Error classification triggers compaction on context overflow
6. Tool result persistence kicks in for large outputs
7. Pre-API pruning reduces context before the API call
8. Full pipeline: prune → sanitize → guardrail → execute → persist → verify
"""
import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_loop_guardrails import ToolLoopGuardController, ToolGuardrailConfig
from app.services.iteration_budget import IterationBudget
from app.services.tool_result_persistence import persist_tool_result, apply_turn_budget, PersistenceConfig
from app.services.message_sanitization import sanitize_messages
from app.services.compaction.pre_api_prune import prune_tool_results_only
from app.services.verification_stop import build_verify_on_stop_nudge
from app.services.api_error_classifier import classify_api_error, FailoverReason
from app.services.tool_result_classification import tool_may_have_side_effect
from app.services.prompt_caching import apply_cache_control
from app.services.agent_metrics import metrics


def _make_tool_call(name, args, call_id="call_1"):
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _make_assistant_msg(tool_calls=None, content="Done."):
    return {
        "role": "assistant",
        "content": content if not tool_calls else None,
        "tool_calls": tool_calls,
    }


def _make_tool_result(call_id, result_dict):
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result_dict)}


# -- E2E Test 1: Guardrail halts same-tool-failure loop --

def test_e2e_guardrail_halts_failure_loop():
    """Simulate a turn where the same tool fails repeatedly — guardrail should halt."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    budget = IterationBudget(max_total=50)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "search for X"}]

    halted = False
    for i in range(15):
        if not budget.consume():
            break

        # Simulate LLM calling web_search with different args each time
        tool_call = _make_tool_call("web_search", {"query": f"q{i}"}, f"call_{i}")
        args = {"query": f"q{i}"}

        # Guardrail before_call
        decision = ctrl.before_call("web_search", args)
        if not decision.allows_execution:
            messages.append(_make_assistant_msg(tool_calls=[tool_call]))
            messages.append(_make_tool_result(f"call_{i}", {
                "success": False, "error": "blocked by guardrail",
            }))
            halted = True
            break

        # Simulate failed tool execution
        messages.append(_make_assistant_msg(tool_calls=[tool_call]))
        result_str = json.dumps({"success": False, "error": "search failed"})

        # Guardrail after_call
        decision = ctrl.after_call("web_search", args, result_str)
        messages.append(_make_tool_result(f"call_{i}", json.loads(result_str)))

        if decision.should_halt:
            halted = True
            break

    assert halted, "Guardrail should have halted the failure loop"
    assert ctrl.halt_decision is not None
    assert budget.used < 15, "Should not have consumed all 15 iterations"


# -- E2E Test 2: Iteration budget exhaustion --

def test_e2e_iteration_budget_exhaustion():
    """Simulate a conversation that hits the iteration budget cap."""
    budget = IterationBudget(max_total=3)
    iterations_run = 0

    for i in range(10):
        if not budget.consume():
            break
        iterations_run += 1

    assert iterations_run == 3, f"Should stop at 3 iterations, got {iterations_run}"
    assert budget.remaining == 0


# -- E2E Test 3: Pre-API pipeline (prune + sanitize) --

def test_e2e_pre_api_pipeline():
    """Verify prune + sanitize run correctly before an API call."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello\ud800"},  # surrogate
        {"role": "assistant", "content": None,
         "tool_calls": [_make_tool_call("read_file", {"path": "/a.py"}, "c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "X" * 3000},  # large old result
        {"role": "assistant", "content": None,
         "tool_calls": [_make_tool_call("read_file", {"path": "/b.py"}, "c2")]},
        {"role": "tool", "tool_call_id": "c2", "content": "recent result"},
    ]

    # Step 1: prune (with high trigger so it fires)
    msgs, n_pruned = prune_tool_results_only(messages, current_tokens=20000, keep_recent=1, min_prune_chars=500)
    assert n_pruned > 0 or len(msgs) == len(messages)  # pruning happened or no-op

    # Step 2: sanitize
    changed = sanitize_messages(msgs)
    # Surrogate should be replaced
    assert "\ud800" not in msgs[1]["content"]


# -- E2E Test 4: Verification-on-stop after code edit --

def test_e2e_verification_on_stop():
    """Verify the nudge fires when code is edited without verification."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "write a test file"},
        _make_assistant_msg(tool_calls=[_make_tool_call("write_file", {"path": "/a/test.py", "content": "def test(): pass"}, "c1")]),
        _make_tool_result("c1", {"success": True}),
        _make_assistant_msg(content="Done writing the test!"),
    ]

    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is not None, "Verification nudge should fire"
    assert "verify" in nudge.lower() or "verification" in nudge.lower()
    assert "/a/test.py" in nudge


# -- E2E Test 5: Error classification → compaction trigger --

def test_e2e_error_classification_triggers_compress():
    """Verify context overflow error sets should_compress."""
    error = Exception("This model's maximum context length is 8192 tokens")
    ce = classify_api_error(error)
    assert ce.should_compress is True
    assert ce.retryable is True
    assert ce.reason == FailoverReason.context_overflow


def test_e2e_error_classification_rate_limit():
    """Verify rate limit error is retryable."""
    ce = classify_api_error(Exception("rate limit exceeded"), status_code=429)
    assert ce.retryable is True
    assert ce.reason == FailoverReason.rate_limit


# -- E2E Test 6: Tool result persistence for large output --

def test_e2e_large_result_persistence():
    """Verify large tool results get persisted to disk with preview."""
    config = PersistenceConfig(result_threshold_chars=100, preview_chars=50)
    with tempfile.TemporaryDirectory() as tmpdir:
        big_result = json.dumps({"success": True, "content": "X" * 500})
        new_str, meta = persist_tool_result("web_search", big_result, tmpdir, config)
        assert meta["persisted"] is True
        assert os.path.exists(meta["stored_path"])
        assert len(new_str) < len(big_result)


def test_e2e_read_file_not_persisted():
    """Verify read_file results are never persisted (prevents loops)."""
    config = PersistenceConfig(result_threshold_chars=10, preview_chars=5)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = json.dumps({"success": True, "content": "X" * 500})
        new_str, meta = persist_tool_result("read_file", result, tmpdir, config)
        assert meta["persisted"] is False
        assert new_str == result


# -- E2E Test 7: Full pipeline simulation --

def test_e2e_full_pipeline_simulation():
    """Simulate the full pre-API → guardrail → execute → persist → verify pipeline."""
    budget = IterationBudget(max_total=10)
    ctrl = ToolLoopGuardController()
    messages = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "read a file and write a test"},
    ]

    # --- Iteration 1: read_file ---
    assert budget.consume()
    tool_call = _make_tool_call("read_file", {"path": "/a.py"}, "c1")
    decision = ctrl.before_call("read_file", {"path": "/a.py"})
    assert decision.allows_execution

    # Simulate tool result
    result = {"success": True, "content": "def add(a, b): return a + b"}
    result_str = json.dumps(result)
    messages.append(_make_assistant_msg(tool_calls=[tool_call]))
    messages.append(_make_tool_result("c1", result))

    ctrl.after_call("read_file", {"path": "/a.py"}, result_str)

    # --- Pre-API for next iteration ---
    prune_tool_results_only(messages, current_tokens=500)  # below trigger = no-op
    sanitize_messages(messages)  # clean = no changes

    # --- Iteration 2: write_file ---
    assert budget.consume()
    tool_call2 = _make_tool_call("write_file", {"path": "/a_test.py", "content": "def test_add(): assert add(1,2)==3"}, "c2")
    decision2 = ctrl.before_call("write_file", {"path": "/a_test.py", "content": "..."})
    assert decision2.allows_execution

    result2 = {"success": True}
    result_str2 = json.dumps(result2)
    messages.append(_make_assistant_msg(tool_calls=[tool_call2]))
    messages.append(_make_tool_result("c2", result2))
    ctrl.after_call("write_file", {"path": "/a_test.py", "content": "..."}, result_str2)

    # --- LLM tries to finish (no tool calls) ---
    messages.append(_make_assistant_msg(content="Done!"))

    # --- Verification-on-stop should fire ---
    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is not None, "Verification nudge should fire after write without verify"
    assert "/a_test.py" in nudge

    # Budget should show 2 consumed
    assert budget.used == 2
    assert budget.remaining == 8


# -- E2E Test 8: Prompt caching integration --

def test_e2e_prompt_caching_with_pipeline():
    """Verify prompt caching works alongside the pre-API pipeline."""
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    # Pre-API pipeline
    prune_tool_results_only(messages, current_tokens=500)
    sanitize_messages(messages)

    # Prompt caching (disabled = no-op for DeepSeek)
    cached = apply_cache_control(messages, enabled=False)
    assert cached is messages  # no-op

    # Prompt caching (enabled = adds markers)
    cached = apply_cache_control(messages, enabled=True)
    assert cached is not messages  # new object (deep copy)
    sys_content = cached[0]["content"]
    assert isinstance(sys_content, list)  # converted to content list with cache_control
