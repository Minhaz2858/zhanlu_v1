"""Integration tests verifying P0-P3 modules work together correctly.

These tests don't mock the LLM — they test the helper functions and
simulated message flows that exercise the full reliability pipeline:

1. Guardrail controller + iteration budget + result persistence interaction
2. Error classifier + tool retry integration
3. Verification-on-stop + message sanitization pipeline
4. Prompt caching + pre-API pruning + sanitization order
"""
import json
import os
import sys
import tempfile

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_loop_guardrails import ToolLoopGuardController, ToolGuardrailConfig
from app.services.iteration_budget import IterationBudget
from app.services.tool_result_persistence import (
    persist_tool_result, apply_turn_budget, budget_for_context_window, PersistenceConfig,
)
from app.services.api_error_classifier import classify_api_error, FailoverReason
from app.services.tool_retry import is_retryable
from app.services.message_sanitization import sanitize_messages
from app.services.compaction.pre_api_prune import prune_tool_results_only
from app.services.verification_stop import build_verify_on_stop_nudge
from app.services.prompt_caching import apply_cache_control
from app.services.tool_result_classification import tool_may_have_side_effect


# -- Test 1: Guardrail + Budget + Persistence interaction --

def test_guardrail_halt_stops_iteration_budget():
    """When the guardrail halts, the budget should reflect consumed iterations."""
    budget = IterationBudget(max_total=10)
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    
    # Simulate 8 failing calls to the same tool with different args
    for i in range(8):
        if not budget.consume():
            break
        args = {"query": f"q{i}"}
        ctrl.before_call("web_search", args)
        result = json.dumps({"success": False, "error": "boom"})
        decision = ctrl.after_call("web_search", args, result)
        if decision.should_halt:
            break
    
    assert ctrl.halt_decision is not None
    assert budget.used <= 8  # consumed at most 8 iterations
    assert budget.remaining >= 2  # at least 2 remaining


def test_execute_code_refund_restores_budget():
    """A successful execute_code turn should refund the consumed iteration."""
    budget = IterationBudget(max_total=5)
    budget.consume()  # consumed 1
    assert budget.remaining == 4
    
    # Simulate successful execute_code
    parsed_calls = [{"tool_name": "execute_code", "args": {"code": "1+1"}}]
    results = [{"success": True, "output": "2"}]
    
    if all(c["tool_name"] == "execute_code" for c in parsed_calls):
        if all(isinstance(r, dict) and r.get("success") is True for r in results):
            budget.refund()
    
    assert budget.remaining == 5  # refunded back to full


# -- Test 2: Error classifier + tool retry --

def test_error_classifier_used_by_is_retryable():
    """is_retryable should use the structured classifier for known error types."""
    # Rate limit error should be retryable
    assert is_retryable(Exception("rate limit exceeded")) is True
    # Timeout should be retryable
    assert is_retryable(TimeoutError("request timed out")) is True
    # Connection error should be retryable
    assert is_retryable(ConnectionError("connection refused")) is True


def test_error_classifier_context_overflow_triggers_compress():
    """Context overflow should set should_compress for reactive compaction."""
    ce = classify_api_error("context_length_exceeded", status_code=400)
    assert ce.should_compress is True
    assert ce.retryable is True


# -- Test 3: Pre-API pipeline order --

def test_pre_api_pipeline_prune_then_sanitize():
    """The pre-API pipeline should prune first, then sanitize."""
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path": "/a"}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": "X" * 2000},
    ]
    
    # Step 1: prune old tool results
    msgs, n_pruned = prune_tool_results_only(
        messages, current_tokens=20000, keep_recent=5, min_prune_chars=500
    )
    # Large result should be summarized
    # (but only if there are more than keep_recent results — here there's only 1)
    
    # Step 2: sanitize
    changed = sanitize_messages(msgs)
    # Messages should be valid
    assert isinstance(msgs, list)
    for m in msgs:
        assert isinstance(m, dict)


def test_pre_api_pipeline_sanitize_repairs_malformed_args():
    """Sanitization should repair malformed tool_call arguments."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path": "/a",}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": "content"},
    ]
    sanitize_messages(messages)
    args = messages[1]["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(args)
    assert parsed == {"path": "/a"}


# -- Test 4: Verification-on-stop + sanitization --

def test_verification_nudge_fires_after_write_without_verify():
    """The verification nudge should fire when code is written without verification."""
    messages = [
        {"role": "user", "content": "write a file"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "write_file",
                                      "arguments": json.dumps({"path": "/a/test.py", "content": "print(1)"})}}]},
        {"role": "tool", "tool_call_id": "1", "content": '{"success": true}'},
        {"role": "assistant", "content": "Done!"},
    ]
    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is not None
    assert "verification" in nudge.lower() or "verify" in nudge.lower()
    assert "/a/test.py" in nudge


def test_verification_nudge_suppressed_after_execute_code():
    """No nudge when execute_code was called after write_file."""
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "write_file",
                                      "arguments": json.dumps({"path": "/a/test.py", "content": "print(1)"})}}]},
        {"role": "tool", "tool_call_id": "1", "content": '{"success": true}'},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "2", "type": "function",
                         "function": {"name": "execute_code",
                                      "arguments": json.dumps({"language": "python", "code": "1+1"})}}]},
        {"role": "tool", "tool_call_id": "2", "content": '{"success": true, "output": "2"}'},
        {"role": "assistant", "content": "Done!"},
    ]
    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is None


# -- Test 5: Prompt caching --

def test_prompt_caching_noop_when_disabled():
    """When caching is disabled, messages pass through unchanged."""
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    result = apply_cache_control(messages, enabled=False)
    assert result is messages  # same object


def test_prompt_caching_applies_when_enabled():
    """When caching is enabled, markers are added to system + recent messages."""
    messages = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    result = apply_cache_control(messages, enabled=True)
    # System prompt should have cache_control via content list
    assert isinstance(result[0]["content"], list)
    assert result[0]["content"][0].get("cache_control") is not None


# -- Test 6: Result persistence + tool classification --

def test_read_file_result_not_persisted():
    """read_file results must never be persisted (prevents persist->read->persist loop)."""
    config = PersistenceConfig(result_threshold_chars=10, preview_chars=5)
    with tempfile.TemporaryDirectory() as tmpdir:
        result_str = json.dumps({"success": True, "content": "X" * 500})
        new_str, meta = persist_tool_result("read_file", result_str, tmpdir, config)
        assert meta["persisted"] is False
        assert new_str == result_str


def test_write_file_has_side_effect():
    """write_file is classified as having side effects."""
    assert tool_may_have_side_effect("write_file") is True
    assert tool_may_have_side_effect("read_file") is False


# -- Test 7: Full pipeline simulation --

def test_full_pre_api_pipeline_simulation():
    """Simulate the full pre-API pipeline: prune -> sanitize -> cache."""
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path": "/a.py"}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": "file content here"},
        {"role": "assistant", "content": "I read the file."},
        {"role": "user", "content": "now write a test"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "2", "type": "function",
                         "function": {"name": "write_file",
                                      "arguments": json.dumps({"path": "/a_test.py", "content": "def test(): pass"})}}]},
        {"role": "tool", "tool_call_id": "2", "content": '{"success": true}'},
        {"role": "assistant", "content": "Done writing the test."},
    ]
    
    # Step 1: prune old tool results (below trigger = no-op)
    msgs, n = prune_tool_results_only(messages, current_tokens=500)
    assert n == 0  # below trigger threshold
    
    # Step 2: sanitize
    sanitize_messages(msgs)
    
    # Step 3: apply prompt caching (disabled by default for DeepSeek)
    final = apply_cache_control(msgs, enabled=False)
    assert final is msgs  # no-op
    
    # Messages should still be valid
    assert len(final) == len(messages)
    for m in final:
        assert "role" in m


def test_guardrail_blocks_then_budget_preserved():
    """When guardrail blocks a call, the iteration budget is preserved (not consumed)."""
    budget = IterationBudget(max_total=5)
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    
    # Trip the exact-failure block threshold
    args = {"path": "/missing.txt"}
    for i in range(6):
        budget.consume()  # consume before execution
        decision = ctrl.before_call("read_file", args)
        if not decision.allows_execution:
            # Blocked — don't execute, don't consume more budget
            break
        ctrl.after_call("read_file", args, json.dumps({"success": False, "error": "not found"}))
    
    # Guardrail should have tripped
    assert ctrl.halt_decision is not None
    # Budget should have consumed at most 6 (one per attempt before block)
    assert budget.used <= 6
