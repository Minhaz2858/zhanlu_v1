"""Performance benchmarks — measure the ROI of reliability features.

Quantifies the token/character savings from:
1. Pre-API pruning (old tool result summarization)
2. Tool result persistence (large result disk spill)
3. Message sanitization overhead (should be negligible)
4. Guardrail controller overhead (should be negligible)
5. Prompt caching (breakpoint application)
6. Context-window-scaled vs flat budgets

Run: python -m pytest tests/test_benchmarks.py -v -s
"""
import json
import os
import sys
import time
import tempfile

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.compaction.pre_api_prune import prune_tool_results_only
from app.services.tool_result_persistence import persist_tool_result, apply_turn_budget, PersistenceConfig, budget_for_context_window
from app.services.message_sanitization import sanitize_messages
from app.services.tool_loop_guardrails import ToolLoopGuardController
from app.services.prompt_caching import apply_cache_control


def _make_tool_msg(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _make_assistant_with_call(call_id, name, args="{}"):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": args}}],
    }


# -- Benchmark 1: Pre-API pruning savings --

def test_benchmark_pre_api_pruning_savings():
    """Measure character savings from pre-API pruning."""
    # Build a conversation with 20 tool results, each 2000 chars
    messages = [{"role": "system", "content": "sys"}]
    for i in range(20):
        messages.append(_make_assistant_with_call(f"c{i}", "read_file"))
        messages.append(_make_tool_msg(f"c{i}", json.dumps({"content": "X" * 2000})))

    total_before = sum(len(m.get("content", "")) for m in messages if isinstance(m.get("content"), str))

    # Prune (keep recent 5, summarize older)
    msgs, n_pruned = prune_tool_results_only(
        messages, current_tokens=50000, keep_recent=5, min_prune_chars=500
    )

    total_after = sum(len(m.get("content", "")) for m in msgs if isinstance(m.get("content"), str))
    savings_pct = (1 - total_after / total_before) * 100 if total_before > 0 else 0

    print(f"\n  Pre-API pruning: {total_before} -> {total_after} chars ({savings_pct:.1f}% savings, {n_pruned} items pruned)")
    assert savings_pct > 50, f"Expected >50% savings, got {savings_pct:.1f}%"


# -- Benchmark 2: Tool result persistence savings --

def test_benchmark_result_persistence_savings():
    """Measure character savings from Layer 2 persistence."""
    config = PersistenceConfig(result_threshold_chars=500, preview_chars=200)
    with tempfile.TemporaryDirectory() as tmpdir:
        # Simulate 5 large tool results
        results = [
            ("web_search", json.dumps({"content": "A" * 5000})),
            ("web_extract", json.dumps({"content": "B" * 3000})),
            ("execute_code", json.dumps({"output": "C" * 2000})),
        ]

        total_before = sum(len(r) for _, r in results)
        persisted_results = []
        for name, result_str in results:
            new_str, meta = persist_tool_result(name, result_str, tmpdir, config)
            persisted_results.append((name, new_str))

        total_after = sum(len(r) for _, r in persisted_results)
        savings_pct = (1 - total_after / total_before) * 100 if total_before > 0 else 0

        print(f"\n  Result persistence: {total_before} -> {total_after} chars ({savings_pct:.1f}% savings)")
        assert savings_pct > 50


# -- Benchmark 3: Message sanitization overhead --

def test_benchmark_sanitization_overhead():
    """Measure the overhead of message sanitization (should be <1ms for typical messages)."""
    messages = [
        {"role": "system", "content": "system prompt " * 100},
        {"role": "user", "content": "hello world " * 50},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path": "/a.py"}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": "file content " * 100},
    ] * 10  # 40 messages

    start = time.perf_counter()
    for _ in range(100):
        sanitize_messages(messages)
    elapsed_ms = (time.perf_counter() - start) * 10  # ms per call

    print(f"\n  Sanitization overhead: {elapsed_ms:.3f}ms per call (100 calls, 40 messages each)")
    assert elapsed_ms < 10, f"Sanitization should be <10ms, got {elapsed_ms:.3f}ms"


# -- Benchmark 4: Guardrail controller overhead --

def test_benchmark_guardrail_overhead():
    """Measure the overhead of guardrail before_call + after_call."""
    ctrl = ToolLoopGuardController()
    args = {"path": "/a.py"}
    result = json.dumps({"success": True, "content": "data"})

    start = time.perf_counter()
    for _ in range(1000):
        ctrl.before_call("read_file", args)
        ctrl.after_call("read_file", args, result)
    elapsed_ms = (time.perf_counter() - start) * 1  # ms per iteration

    print(f"\n  Guardrail overhead: {elapsed_ms:.3f}ms per before+after call (1000 iterations)")
    assert elapsed_ms < 1, f"Guardrail should be <1ms per call, got {elapsed_ms:.3f}ms"


# -- Benchmark 5: Prompt caching overhead --

def test_benchmark_prompt_caching_overhead():
    """Measure the overhead of apply_cache_control."""
    messages = [
        {"role": "system", "content": "system prompt " * 50},
        {"role": "user", "content": "hello " * 20},
        {"role": "assistant", "content": "reply " * 20},
    ] * 5  # 15 messages

    start = time.perf_counter()
    for _ in range(100):
        apply_cache_control(messages, enabled=True)
    elapsed_ms = (time.perf_counter() - start) * 10

    print(f"\n  Prompt caching overhead: {elapsed_ms:.3f}ms per call (100 calls, 15 messages)")
    assert elapsed_ms < 50, f"Prompt caching should be <50ms, got {elapsed_ms:.3f}ms"


# -- Benchmark 6: Context-window-scaled vs flat budgets --

def test_benchmark_context_scaled_budgets():
    """Compare context-window-scaled budgets vs flat defaults."""
    # Small model (8K context)
    small = budget_for_context_window(8192)
    # Large model (200K context)
    large = budget_for_context_window(200000)
    # Default (no context info)
    default = budget_for_context_window(None)

    print(f"\n  Context-scaled budgets:")
    print(f"    8K model:  per_result={small.result_threshold_chars}, per_turn={small.turn_budget_chars}")
    print(f"    200K model: per_result={large.result_threshold_chars}, per_turn={large.turn_budget_chars}")
    print(f"    Default:   per_result={default.result_threshold_chars}, per_turn={default.turn_budget_chars}")

    # Small model should have smaller budgets (floored)
    assert small.result_threshold_chars <= large.result_threshold_chars
    assert small.turn_budget_chars <= large.turn_budget_chars
    # But never below the floor
    assert small.result_threshold_chars >= 4000
    assert small.turn_budget_chars >= 16000
    # Large model should use defaults (capped)
    assert large.result_threshold_chars == default.result_threshold_chars
    assert large.turn_budget_chars == default.turn_budget_chars


# -- Benchmark 7: Turn budget (Layer 3) spill savings --

def test_benchmark_turn_budget_spill():
    """Measure savings from Layer 3 turn budget spill."""
    config = PersistenceConfig(
        result_threshold_chars=100000,  # high so Layer 2 doesn't fire
        preview_chars=200,
        turn_budget_chars=5000,  # low to trigger Layer 3
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        results = [
            ("web_search", json.dumps({"content": "A" * 3000})),
            ("web_extract", json.dumps({"content": "B" * 2000})),
            ("execute_code", json.dumps({"output": "C" * 2000})),
            ("web_search", json.dumps({"content": "D" * 1500})),
        ]

        total_before = sum(len(r) for _, r in results)
        spilled = apply_turn_budget(results, tmpdir, config)
        total_after = sum(len(r) for _, r in spilled)
        savings_pct = (1 - total_after / total_before) * 100 if total_before > 0 else 0

        print(f"\n  Turn budget spill: {total_before} -> {total_after} chars ({savings_pct:.1f}% savings)")
        assert savings_pct > 0, "Turn budget should reduce total size"
