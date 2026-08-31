"""Tests that _force_llm_synthesis uses the CEO-grade prompt for qwen3.

Root cause (user screenshot 2026-08-25):
- qwen3.6-27b generated a partial "Contract Performance Report":
  - Executive Summary (rich, 6-7 sentences with numbers)
  - Key Metrics (empty table header)
  - (then stopped — no Detailed Breakdown, no Risks, no Recommendations)
- Total: ~1000 tokens, well below any max_tokens cap
- The LLM was told to "Write a COMPREHENSIVE 5-8 sentence analysis" (line 650)
- For qwen3 with rich data, 5-8 sentences is too short — the LLM produces
  a partial report and stops.

Fix: When the endpoint is qwen3, _force_llm_synthesis must use the
CEO-grade prompt (5 sections: Executive Summary, Key Metrics, Detailed
Breakdown, Risks & Opportunities, Recommended Actions). The qwen3 prompt
function is _build_qwen3_synthesis_prompt at line 1448, defined but
never called.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-v', 'tests/test_qwen3_ceo_grade_prompt_wiring.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Test 1: _force_llm_synthesis accepts an endpoint parameter ──────────────


def test_force_llm_synthesis_accepts_endpoint():
    """The function must accept an endpoint parameter so it can detect
    qwen3 and choose the right prompt."""
    import inspect
    from app.routers.agents import _force_llm_synthesis
    sig = inspect.signature(_force_llm_synthesis)
    assert "endpoint" in sig.parameters, (
        "_force_llm_synthesis must accept an endpoint parameter so it can "
        "use the qwen3-specific CEO-grade prompt for qwen3.6-27b users. "
        "Currently no way to detect the model from inside the function."
    )


# ── Test 2: A qwen3-suffixed sentinel endpoint is detected as qwen3 ────────


def test_qwen3_endpoint_detected():
    """The endpoint detection must work for qwen3.6-27b endpoints. Use a
    minimal sentinel object to verify the detection logic without needing
    a real Endpoint ORM object."""
    from app.routers.agents import _is_qwen_local_model

    class FakeEndpoint:
        model_id = "qwen3.6-27b"

    assert _is_qwen_local_model(FakeEndpoint()) is True

    class NonQwenEndpoint:
        model_id = "deepseek-chat"

    assert _is_qwen_local_model(NonQwenEndpoint()) is False


# ── Test 3: _build_qwen3_synthesis_prompt is wired in (not orphan) ─────────


def test_qwen3_synthesis_prompt_referenced_in_force_synthesis():
    """The qwen3-specific CEO-grade prompt must be reachable from inside
    _force_llm_synthesis. Use a static source check: the function must
    either call _build_qwen3_synthesis_prompt directly, or build a prompt
    string containing the 5 section headers."""
    import inspect
    from app.routers.agents import _force_llm_synthesis
    src = inspect.getsource(_force_llm_synthesis)
    has_ceo_template = (
        "Executive Summary" in src
        and "Key Metrics" in src
        and "Detailed Breakdown" in src
        and "Risks & Opportunities" in src
        and "Recommended Actions" in src
    )
    assert has_ceo_template, (
        "_force_llm_synthesis must include the CEO-grade 5-section template "
        "(Executive Summary, Key Metrics, Detailed Breakdown, "
        "Risks & Opportunities, Recommended Actions) for qwen3.6-27b. "
        "Currently the function only has a 5-8 sentence prompt that "
        "truncates qwen3's output mid-report (user screenshot 2026-08-25)."
    )


# ── Test 4: Prompt no longer says "5-8 sentence analysis" ─────────────────


def test_prompt_does_not_constrain_to_5_8_sentences():
    """The old prompt said 'Write a COMPREHENSIVE 5-8 sentence analysis'.
    This cap causes qwen3 to stop mid-report. The QWEN3-SPECIFIC prompt
    must NOT have this constraint (legacy non-qwen3 path is fine)."""
    import inspect
    from app.routers.agents import _force_llm_synthesis
    src = inspect.getsource(_force_llm_synthesis)
    # Find the qwen3 branch specifically
    qwen3_branch = src.split("if _is_qwen:")[1].split("elif attempt == 0:")[0]
    assert "5-8 sentence" not in qwen3_branch, (
        "The qwen3 branch in _force_llm_synthesis must NOT tell the LLM to "
        "write only 5-8 sentences. This cap causes qwen3 to truncate after "
        "the Executive Summary. Replace with the CEO-grade 5-section template."
    )
    # Also verify the CEO-grade template is actually used for qwen3
    assert "Executive Summary" in qwen3_branch
    assert "Key Metrics" in qwen3_branch
    assert "Detailed Breakdown" in qwen3_branch
    assert "Risks & Opportunities" in qwen3_branch
    assert "Recommended Actions" in qwen3_branch


# ── Test 5: Non-qwen3 endpoints still get the legacy prompt ────────────────


def test_non_qwen3_keeps_legacy_prompt_path():
    """For deepseek-chat and other non-qwen3 models, the function must
    still produce SOMETHING useful. Verify the legacy 5-8 sentence prompt
    is preserved as a fallback."""
    import inspect
    from app.routers.agents import _force_llm_synthesis
    src = inspect.getsource(_force_llm_synthesis)
    # Should have BOTH the qwen3 CEO-grade path AND a legacy/fallback path
    assert "endpoint" in src, (
        "Need an endpoint parameter to branch on qwen3 vs other models."
    )
    # The function must still be able to build a prompt for non-qwen3
    # (either keep the legacy prompt, or default to a generic one)
    has_generic = (
        "5-8" in src  # legacy
        or "data analyst" in src  # generic
        or "comprehensive" in src.lower()  # new generic
    )
    assert has_generic, (
        "_force_llm_synthesis needs a fallback prompt for non-qwen3 models."
    )
