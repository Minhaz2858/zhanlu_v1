"""Tests for qwen3.6-27b context-window auto-compact fix.

Bug repro (Aug 2026): user runs a long conversation under qwen3.6-27b
(``context_window=65536`` from the DB row).  Auto-compact never fires
pre-emptively because the pre-call check at agents.py:4123 / agents.py:9829
uses the *global default* model (``settings.LLM_MODEL`` = deepseek-v4-flash,
128k context) to resolve the context window via ``get_context_window``.
``should_autocompact`` thus sees a 128k budget, computes a 70 % trigger of
89,600 tokens, and stays asleep while the conversation drifts to 61,441
tokens.  The next user turn pushes input + 4,096 requested output over the
65,536 ceiling -> 400 "maximum context length is 65536 tokens" ->
reactive compaction runs but doesn't free enough -> the user sees
"Sorry, I hit an error while responding."

These tests pin the two parts of the fix:

1. ``get_context_window("qwen3.6-27b")`` MUST return a value <= 70_000
   (the DB row's ``context_window=65_536``).  The current heuristic
   returns 128_000 which is the root cause.
2. ``should_autocompact`` MUST fire at 61k tokens when called with
   ``context_window_tokens=65_536`` and the user's selected model.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.compaction import (
    AutoCompactState,
    get_context_window,
    should_autocompact,
)


def test_qwen36_27b_context_window_is_not_128k():
    """qwen3.6-27b has DB context_window=65536, the heuristic must respect that.

    If this returns 128_000 (the cloud qwen default), auto-compact will
    think the model has 89.6k of headroom and never fire pre-emptively for
    conversations under 90k tokens — but the real model only has 65,536.
    """
    cw = get_context_window("qwen3.6-27b")
    assert cw <= 70_000, (
        f"qwen3.6-27b has DB context_window=65536 but get_context_window "
        f"returns {cw}; auto-compact threshold will be too high and the "
        f"conversation will overflow the model's real limit"
    )
    # And the model is definitely not a 128k model:
    assert cw != 128_000, (
        f"qwen3.6-27b should not be treated as a 128k model; got {cw}"
    )


def test_should_autocompact_fires_for_qwen36_27b_near_limit():
    """At 61,441 tokens with the user's qwen3.6-27b (65,536), auto-compact
    MUST return True so the conversation is compacted BEFORE the next turn
    overflows the model.
    """
    # Simulate the conversation state: ~60k tokens of large user/assistant
    # content.  The estimator is chars/4, so 240k chars -> 60k tokens.
    big_text = "x" * 240_000
    messages = [
        {"role": "user", "content": big_text},
        {"role": "assistant", "content": big_text},
    ]
    state = AutoCompactState()
    # Caller passes the user's model AND its real context_window from the
    # DB row.  This is what the fix wires up at agents.py:4123 / 9829.
    result = should_autocompact(
        messages,
        "qwen3.6-27b",
        state,
        context_window_tokens=65_536,
    )
    assert result is True, (
        "should_autocompact must fire for qwen3.6-27b at 60k tokens when "
        "context_window_tokens=65536; without this, the next turn overflows"
    )


def test_should_autocompact_does_not_fire_under_threshold_for_qwen36_27b():
    """A small qwen3.6-27b conversation MUST NOT trigger auto-compact."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    state = AutoCompactState()
    result = should_autocompact(
        messages,
        "qwen3.6-27b",
        state,
        context_window_tokens=65_536,
    )
    assert result is False


def test_get_context_window_model_context_windows_takes_precedence():
    """If ``MODEL_CONTEXT_WINDOWS`` config has an entry for the model, it
    must win over the name-based heuristic.
    """
    # The config has 'qwen3.5-27b': 8192; verify it wins over the qwen
    # cloud default of 128k.
    cw = get_context_window("qwen3.5-27b")
    assert cw == 8_192, f"expected 8192 from config, got {cw}"


def test_get_context_window_default_for_unknown_qwen():
    """For an unknown qwen model, the heuristic returns 128k (cloud default).

    This is fine because users can override via DB row / config map.
    """
    cw = get_context_window("qwen-unknown-future-model")
    assert cw == 128_000
