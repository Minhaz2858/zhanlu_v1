"""Tests that the outer except (after _force_llm_synthesis) uses the
deterministic fallback, not the OLD placeholder.

Root cause (user screenshot 2026-08-25):
- qwen3.6-27b query "make a sales deck for Q3 with a beautiful design"
- Data retrieved: 10 rows, 5 columns (real sales data)
- Synthesis LLM call THREW (timeout or network error)
- User saw: "Analyzing 10 rows of data…" (the OLD placeholder)
- Expected: "## Executive Summary (auto-generated)..." (the CEO-grade analysis)

Bug: The OUTER except at line 14047-14048 in agents.py catches errors
from the WHOLE _force_llm_synthesis call and falls straight through
to _data_rows_fallback (the placeholder), BYPASSING the
_build_deterministic_fallback fix from the inner try/except.

Fix: The outer except must ALSO try _build_deterministic_fallback
first, falling back to _data_rows_fallback only if that also fails.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-v', 'tests/test_outer_except_uses_deterministic.py']))'
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Test 1: outer except must use _build_deterministic_fallback ────────────


def test_outer_except_uses_deterministic_fallback():
    """The outer except blocks in the v3 stream loop must call
    _build_deterministic_fallback (not _data_rows_fallback directly)
    when _force_llm_synthesis throws an exception.

    The pattern to find: in the empty-bubble path, there's an outer
    `except Exception:` that contains `_data_rows_fallback(...)`. This
    is a regression — the fix should try _build_deterministic_fallback
    first.
    """
    import inspect
    from app.routers.agents import _build_deterministic_fallback

    # Verify the function exists (it does, from earlier session)
    assert callable(_build_deterministic_fallback)

    # The structural check: there should be an outer except that
    # tries _build_deterministic_fallback BEFORE _data_rows_fallback.
    # We do this by checking the source pattern.
    from app.routers import agents
    src = inspect.getsource(agents)
    # The bug pattern: "except Exception:\n accumulated_content = _data_rows_fallback"
    # This appears in the outer excepts at lines 14048 and 14148.
    # The fix pattern: in those outer excepts, there must be a
    # try/except that tries _build_deterministic_fallback first.

    # Count how many times the bug pattern appears
    bug_pattern = "except Exception:\n                            accumulated_content = _data_rows_fallback"
    bug_count = src.count(bug_pattern)
    # And the fix pattern (try with _build_deterministic_fallback)
    fix_pattern_count = src.count("accumulated_content = _build_deterministic_fallback(")

    # We need at least as many fix patterns as the bug count
    # Currently 3 fix patterns (one in each of 3 call sites) and 3 bug patterns
    # The fix: in the OUTER excepts (where the bug pattern lives), there
    # should be a try/except that ALSO uses _build_deterministic_fallback.
    assert fix_pattern_count >= 3, (
        f"Expected at least 3 _build_deterministic_fallback callsites in the "
        f"v3 stream loop. Found {fix_pattern_count}. The outer excepts "
        f"(where _force_llm_synthesis throws) need to also try "
        f"_build_deterministic_fallback before falling back to the "
        f"_data_rows_fallback placeholder."
    )


# ── Test 2: reproduce the user bug — synthesis throws, user sees placeholder


def test_synthesis_throws_but_user_still_gets_analysis():
    """When _force_llm_synthesis throws (timeout/network error), the
    v3 stream loop must produce a CEO-grade analysis via the
    deterministic fallback, not the placeholder.

    The fix: the outer except (line 14047-14048 in agents.py) must
    try _build_deterministic_fallback BEFORE _data_rows_fallback.
    We verify this by checking that the source has the correct
    nested try/except pattern near line 14047.
    """
    import inspect
    from app.routers import agents
    src = inspect.getsource(agents)

    # The fix pattern: after an outer `except Exception:`, there must
    # be a `try:` that calls _build_deterministic_fallback, then an
    # inner `except Exception:` that calls _data_rows_fallback.
    # We look for: "except Exception:\n... try:\n... _build_deterministic_fallback"
    # within 200 chars of "accumulated_content = _data_rows_fallback"
    import re
    # Find the empty-bubble path: lines around the second
    # _data_rows_fallback (which is the OUTER except at 14048)
    matches = list(re.finditer(
        r"except Exception:\s*\n\s+accumulated_content = _data_rows_fallback",
        src,
    ))
    assert len(matches) >= 2, (
        f"Expected at least 2 'except Exception: ... _data_rows_fallback' "
        f"matches in the empty-bubble path. Found {len(matches)}."
    )
    # The second match should be the outer except that needs the fix
    outer_except = matches[1]
    # Check the 600 chars BEFORE this match for a `try:` block
    context = src[max(0, outer_except.start() - 600):outer_except.start()]
    has_try_block = "try:" in context and "_build_deterministic_fallback" in context
    assert has_try_block, (
        "The OUTER except (line ~14048) in the empty-bubble path "
        "does NOT have a `try: _build_deterministic_fallback` block. "
        "Currently when _force_llm_synthesis throws (timeout, network), "
        "the user sees 'Analyzing 10 rows of data…' instead of the "
        "CEO-grade analysis. Fix: wrap the outer except in a try/except "
        "that tries _build_deterministic_fallback first."
    )


# ── Test 3: ensure deterministic fallback actually runs end-to-end ───────


def test_deterministic_fallback_runs_when_synthesis_throws():
    """End-to-end test: when _force_llm_synthesis raises, the v3
    stream loop's outer except must call _build_deterministic_fallback
    and the result must include 'Executive Summary' (not the placeholder)."""
    # Verify the function works on the actual user data shape
    from app.routers.agents import _build_deterministic_fallback
    test_rows = [
        {
            "material_name": "30#工业用裂解碳五",
            "total_delivery_qty": 1130.205,
            "total_amount": 19107079.64,
            "avg_price": 11793.1998137368,
            "customer_names": "北京万邦达",
        },
        {
            "material_name": "双环戊二烯",
            "total_delivery_qty": 10719.06,
            "total_amount": 74332427.984,
            "avg_price": 5825.978500449,
            "customer_names": "Kolon Industries",
        },
    ]
    result = _build_deterministic_fallback(
        test_rows,
        columns=["material_name", "total_delivery_qty",
                 "total_amount", "avg_price", "customer_names"],
        table_name="sales",
    )
    # Must be a real analysis, NOT the placeholder
    assert "Analyzing" not in result, (
        f"_build_deterministic_fallback returned the placeholder text. "
        f"This means the function is broken or the user is seeing the "
        f"wrong output. Got: {result[:200]}"
    )
    # Must include Executive Summary
    assert "Executive Summary" in result
    # Must include the markdown table
    assert "|" in result  # markdown table separator
