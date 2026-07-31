"""Tests for Claude-style phase headline SSE events.

The ``phase`` event carries { type, state, verb, title } and is emitted
on FSM transitions and ReAct milestones (turn start, first tool batch,
final response). The frontend renders the latest phase as a headline
above the activity steps (the "✳ Fathoming…" pattern).
"""

from __future__ import annotations

import json

import pytest


def _parse_sse(chunk: str) -> list[dict]:
    events: list[dict] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            events.append(json.loads(line[len("data: "):]))
        except json.JSONDecodeError:
            continue
    return events


class TestEmitPhase:
    def test_basic_phase_payload(self):
        from app.routers.agents import _emit_phase
        events = _parse_sse(_emit_phase("goal"))
        assert len(events) == 1
        evt = events[0]
        assert evt["type"] == "phase"
        assert evt["state"] == "goal"
        assert evt["verb"] == "Fathoming"
        assert evt["title"]  # non-empty default title

    def test_detail_overrides_default_title(self):
        from app.routers.agents import _emit_phase
        evt = _parse_sse(_emit_phase("act", detail="Building the sales deck"))[0]
        assert evt["verb"] == "Fabricating"
        assert evt["title"] == "Building the sales deck"

    def test_unknown_state_falls_back_gracefully(self):
        from app.routers.agents import _emit_phase
        evt = _parse_sse(_emit_phase("some_custom_state"))[0]
        assert evt["type"] == "phase"
        assert evt["verb"] == "Working"
        assert evt["title"]  # humanized fallback, not empty

    def test_all_fsm_states_have_headlines(self):
        from app.routers.agents import PHASE_HEADLINES
        from app.models.execution import FSM_STATES
        for state in FSM_STATES:
            assert state in PHASE_HEADLINES, f"missing phase headline for {state}"
            verb, title = PHASE_HEADLINES[state]
            assert verb and isinstance(verb, str)
            assert title and isinstance(title, str)

    def test_verbs_are_distinct_per_phase(self):
        """Claude's pattern is a *varied* verb vocabulary — not one word
        repeated for every phase."""
        from app.routers.agents import PHASE_HEADLINES
        verbs = [v for v, _ in PHASE_HEADLINES.values()]
        # At least 5 distinct verbs across the lifecycle.
        assert len(set(verbs)) >= 5

    def test_fail_state_has_headline(self):
        from app.routers.agents import _emit_phase
        evt = _parse_sse(_emit_phase("fail"))[0]
        assert evt["verb"]  # something honest like "Stopped"
        assert evt["verb"] != "Done"


class TestActivityStepDetailFields:
    def test_emit_step_with_command_and_output(self):
        from app.routers.agents import _emit_activity_step
        raw = _emit_activity_step(
            3, "Running the provided code", "done",
            tool_name="execute_code",
            command="print('hello')",
            output_preview="hello",
        )
        step = _parse_sse(raw)[0]["step"]
        assert step["command"] == "print('hello')"
        assert step["output_preview"] == "hello"

    def test_emit_step_with_artifact_id(self):
        from app.routers.agents import _emit_activity_step
        raw = _emit_activity_step(
            4, "Creating the deck", "done",
            tool_name="create_artifact", artifact_id="art-123",
        )
        step = _parse_sse(raw)[0]["step"]
        assert step["artifact_id"] == "art-123"

    def test_optional_fields_omitted_when_none(self):
        from app.routers.agents import _emit_activity_step
        raw = _emit_activity_step(1, "Understanding your request", "running")
        step = _parse_sse(raw)[0]["step"]
        assert "command" not in step
        assert "output_preview" not in step
        assert "artifact_id" not in step


class TestPhaseWiredIntoStream:
    """Source-level guards: the phase emitter must actually be called
    from the stream paths (FSM transitions + ReAct milestones)."""

    def test_fsm_state_callback_emits_phase(self):
        import inspect
        import app.routers.agents as agents_mod
        src = inspect.getsource(agents_mod)
        assert "collected.append(_emit_phase(state))" in src

    def test_react_path_emits_opening_phase(self):
        import inspect
        import app.routers.agents as agents_mod
        src = inspect.getsource(agents_mod)
        assert '_emit_phase("goal")' in src

    def test_first_tool_batch_emits_act_phase(self):
        import inspect
        import app.routers.agents as agents_mod
        src = inspect.getsource(agents_mod)
        assert '_emit_phase("act")' in src

    def test_finalize_phase_emitted_before_response(self):
        import inspect
        import app.routers.agents as agents_mod
        src = inspect.getsource(agents_mod)
        assert '_emit_phase("finalize")' in src
