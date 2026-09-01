"""Tests for model-aware compaction (context-window driven, all models).

The compaction system is generic: thresholds are derived from the model's
real context window via ``get_context_window(model)`` — no per-model
hardcoded settings. This file verifies:
- get_context_window maps small local models (qwen3.6-27b 65k) and cloud
  models (deepseek, kimi, generic qwen) to sane windows
- autocompact fires EARLIER on small windows (so the next LLM call fits)
- tool-result pruning is model-aware (trigger scales to the window)
"""


# ── Test 1: get_context_window returns sane values for all model families ──

def test_get_context_window_model_families():
    from app.services.compaction import get_context_window

    # Small local vLLM model: real 65,536 window (non-awq4)
    assert get_context_window("qwen3.6-27b") == 65_536
    # Cloud qwen / moonshot / kimi family
    assert get_context_window("qwen3.5-72b") == 128_000
    assert get_context_window("moonshot-kimi") == 128_000
    # DeepSeek
    assert get_context_window("deepseek-chat") == 128_000
    # Claude family
    assert get_context_window("claude-sonnet") == 200_000
    # Unknown / empty falls back to a CONSERVATIVE default (32K) — never
    # the cloud 128K assumption.  A brand-new/unknown model with a small
    # real window (Ollama 8k, vLLM 16k, custom 4k) MUST compact early and
    # fit, not overflow the next LLM call with a 400.  Known families keep
    # their exact windows (heuristics above); the auto-probe overrides the
    # default with the endpoint's REAL window when one is exposed.
    assert get_context_window("") == 32_000
    assert get_context_window("brand-new-model-2026") == 32_000


# ── Test 2: autocompact fires earlier on small windows ───────────────────

def test_autocompact_fires_early_for_small_window():
    from app.services.compaction import should_autocompact, AutoCompactState

    # ~53K tokens of content (est. 26.7K at 160K chars → 2x → ~53K)
    big_content = "x" * 80_000
    messages = [
        {"role": "system", "content": big_content * 4},  # ~53K tokens
    ]
    state = AutoCompactState()

    # Small window (qwen3.6-27b, 65K): MUST fire — threshold ≈ 65K - 20K - 13K ≈ 32K
    qwen3_should = should_autocompact(
        messages, "qwen3.6-27b", state, context_window_tokens=65_536,
    )
    assert qwen3_should is True, (
        "40K tokens on a 65K window must auto-compact before overflow."
    )

    # Large window (deepseek, 200K): must NOT fire at 40K
    state2 = AutoCompactState()
    ds_should = should_autocompact(
        messages, "deepseek-chat", state2, context_window_tokens=200_000,
    )
    assert ds_should is False, (
        "40K tokens on a 200K window must NOT auto-compact yet."
    )


# ── Test 3: autocompact threshold scales with window size ────────────────

def test_autocompact_threshold_scales():
    from app.services.compaction import get_autocompact_threshold

    small = get_autocompact_threshold("qwen3.6-27b", context_window_tokens=65_536)
    large = get_autocompact_threshold("deepseek-chat", context_window_tokens=200_000)

    # Small window must compact well before 65K (headroom for the summary)
    assert small < 65_536 * 0.8
    # Large window can wait longer (still well under 200K)
    assert large < 200_000 * 0.9
    # And the small window fires strictly earlier than the large one
    assert small < large


# ── Test 4: prune trigger scales to small windows ────────────────────────

def test_prune_trigger_scales_to_window():
    from app.services.compaction.pre_api_prune import _effective_trigger_tokens

    # Small window: trigger at ~50% (≈ 32K for 65K)
    small_trigger = _effective_trigger_tokens(16_000, "qwen3.6-27b")
    assert 2_000 <= small_trigger <= 40_000
    # Large window: keep the flat default (16K)
    large_trigger = _effective_trigger_tokens(16_000, "deepseek-chat")
    assert large_trigger == 16_000


# ── Test 5: prune protects recent results regardless of model ────────────

def test_prune_tool_results_protects_recent_for_all_models():
    from app.services.compaction.pre_api_prune import prune_tool_results_only

    messages = []
    for i in range(5):
        messages.append({
            "role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": "ask_data_agent", "arguments": "{}"}}
            ],
        })
        messages.append({
            "role": "tool", "tool_call_id": f"call_{i}",
            "content": f"unique_payload_{i}_" + ("y" * 1500),
        })

    # Same behavior for both small-local and cloud models at 20K tokens:
    # all 5 results are within keep_recent=5, so nothing is pruned.
    for model in ("qwen3.6-27b", "deepseek-chat", ""):
        result, n_pruned = prune_tool_results_only(
            messages, current_tokens=20_000, model=model,
        )
        assert n_pruned == 0, (
            f"model={model!r}: with keep_recent=5 and 5 unique tool results "
            f"nothing must be pruned at 20K tokens. Got n_pruned={n_pruned}."
        )


# ── Test 6: prune DOES fire when tokens cross the model-scaled trigger ───

def test_prune_fires_when_over_small_window_trigger():
    from app.services.compaction.pre_api_prune import (
        prune_tool_results_only, DEFAULT_KEEP_RECENT,
    )

    messages = []
    for i in range(6):
        messages.append({
            "role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": "ask_data_agent", "arguments": "{}"}}
            ],
        })
        messages.append({
            "role": "tool", "tool_call_id": f"call_{i}",
            "content": f"unique_payload_{i}_" + ("y" * 2000),
        })

    # On a small window the trigger is ~32K; pass 60K so pruning must fire.
    result, n_pruned = prune_tool_results_only(
        messages, current_tokens=60_000, model="qwen3.6-27b",
    )
    # keep_recent=5 protects the last 5; the 1st result is eligible.
    assert n_pruned >= 1, (
        "At 60K tokens on a 65K-window model, old tool results must be "
        f"summarized/pruned. Got n_pruned={n_pruned}."
    )
    assert DEFAULT_KEEP_RECENT == 5
