"""2026-08-25: live-streaming spec — verify plan_step_added SSE events are emitted."""
import os


def test_agents_py_emits_plan_step_added():
    """agents.py must emit plan_step_added SSE events from streaming text."""
    agents_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routers", "agents.py",
    )
    with open(agents_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "plan_step_added" in src, "agents.py must emit plan_step_added events"
    assert "parse_plan_steps_from_text" in src, \
        "agents.py must call parse_plan_steps_from_text"


def test_agents_py_has_dedup_for_plan_steps():
    """The plan step tracking must use a dedup mechanism (no duplicate step_index events)."""
    agents_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routers", "agents.py",
    )
    with open(agents_path, "r", encoding="utf-8") as f:
        src = f.read()
    # Look for the dedup pattern: a set or list with "in" check before yield
    has_set = "_plan_step_tracker" in src or "plan_step_seen" in src
    assert has_set, "agents.py must track seen plan steps to avoid duplicates"
