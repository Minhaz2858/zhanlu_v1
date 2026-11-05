"""2026-08-31: plan steps + plan/finalize phases must PERSIST into live_events.

Bug: plan_step_added / plan_step_completed SSE frames were raw-only — never
appended to ``_live_events`` (the persisted typed feed). After reload the
same assistant message degraded from the rich 4-step plan checklist the user
saw live to a generic "2 EVENTS · 2 COMPLETED" chip. This test locks in the
persistence contract so the reloaded message renders identically to the live
stream (parity with Kimi/GPT/Claude activity feeds).
"""
import os

_AGENTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "routers", "agents.py",
)
_SSE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "services", "agent_loop", "sse_builders.py",
)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_plan_steps_persist_via_push_live_event():
    """plan_step_added must be pushed through _push_live_event (persisted),
    not only yielded as a raw SSE frame."""
    src = _read(_AGENTS)
    # The plan block must persist each step AFTER the raw frame.
    assert 'yield plan_step_added_frame(_st)' in src
    assert '_push_live_event(\n                            "plan_step_added", "plan_step_added"' in src or \
        '"plan_step_added", "plan_step_added"' in src
    # Completed steps must persist too (both loop-evidence and final-mark sites).
    assert src.count('"plan_step_completed", "plan_step_completed"') >= 2, \
        "plan_step_completed must be persisted at BOTH completion sites"


def test_plan_and_finalize_phases_persist():
    """phase_enter.plan and phase_enter.finalize must be persisted so the
    reloaded headline shows the real phase journey (Laying out the plan →
    Crystallizing / Wrapping everything up), not just the final state prop."""
    src = _read(_AGENTS)
    assert '_push_live_event("phase_enter", "phase_enter.plan")' in src
    assert '_push_live_event("phase_enter", "phase_enter.finalize")' in src


def test_plan_step_titles_go_through_content_scrubber():
    """Security closure audit: plan step titles may be LLM-AUTHORED
    (parse_dynamic_plan) — they are raw model text, NOT server-template
    metadata like tool labels. The persisted plan-step pushes must therefore
    use the DEFAULT content scrubber (the old sanitize=False premise leaked
    identifiers echoed by the planner). Ordinary titles still render
    verbatim — the sanitizer only replaces forbidden patterns."""
    src = _read(_AGENTS)
    assert "sanitize=False" not in src, \
        "no live-event site may bypass the content scrubber (plan titles are model text)"
    sse = _read(_SSE)
    assert "sanitize: bool = True" in sse, \
        "_build_live_event must accept a sanitize flag"
    assert "if sanitize else dict(params or {})" in sse, \
        "_build_live_event must keep supporting explicit sanitize control"
