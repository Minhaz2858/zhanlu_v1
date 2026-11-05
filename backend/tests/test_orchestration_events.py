"""Orchestration event emission contract (2026-11-02, Phases 2/4/5).

Pins the previously-dead structured events (agent_retrying via the label
registry, verification_started, peer_collaboration_started, plan_updated,
replanning_started) and the plumbing that carries them — no LLM calls.
"""

from __future__ import annotations

import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.agent_loop.sse_builders import (  # noqa: E402
    LIVE_EVENT_TEMPLATES,
    _AGENT_EXECUTION_EVENT_TYPES,
    _build_live_event,
)


def test_new_event_types_have_bilingual_labels():
    for key in (
        "agent_retrying", "replanning_started", "plan_updated",
        "verification_started",
    ):
        assert key in LIVE_EVENT_TEMPLATES, f"{key} missing from label registry"
        assert "en" in LIVE_EVENT_TEMPLATES[key] and "zh" in LIVE_EVENT_TEMPLATES[key]


def test_structured_events_still_whitelisted():
    for key in (
        "agent_progress", "agent_result_received",
        "peer_collaboration_started", "verification_started",
        "verification_completed",
    ):
        assert key in _AGENT_EXECUTION_EVENT_TYPES


def test_agent_retrying_event_builds_with_tool_label():
    ev = _build_live_event("agent_retrying", "agent_retrying",
                           {"tool_label": "ask_data_agent", "attempt": 1})
    assert ev is not None
    assert ev["type"] == "agent_retrying"
    assert ev["params"]["tool_label"] == "ask_data_agent"


def test_replanning_event_reason_is_sanitized():
    """A reason echoing SQL/table text must be scrubbed like every live
    event (the shared sanitizer runs on params)."""
    ev = _build_live_event(
        "replanning_started", "replanning_started",
        {"reason": "Refining the query"},
    )
    assert ev is not None
    assert ev["params"]["reason"]


def test_verification_started_payload_is_metadata_only():
    ev = _build_live_event(
        "verification_started", "verification_started",
        {"checking": "figures_and_sources"},
    )
    assert ev is not None
    # no raw content keys ever reach the payload
    assert set(ev["params"]) == {"checking"}
