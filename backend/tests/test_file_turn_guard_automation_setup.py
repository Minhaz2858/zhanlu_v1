"""File-turn-guard × automation configuration (RC3).

The definition's ``output_format`` (e.g. "HTML report" in the creation
prefill) must NEVER force ``create_artifact`` during an automation
configuration turn. Normal chat file requests keep the forcing behavior.
"""
from __future__ import annotations

import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402

from app.services.file_turn_guard import (  # noqa: E402
    build_file_nudge,
    should_force_create_file,
)

_CREATION_MESSAGE = (
    "Create a new Automation Task:\n"
    "- Name：Daily Sales Data Sync\n"
    "- Schedule：0 8 * * *\n"
    "- Output format：HTML report (html)\n"
    "- Description：Sync ERP sales data\n"
)


@pytest.fixture
def guard_on(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "FILE_TURN_GUARD_ENABLED", True)


def test_automation_setup_turn_never_forced_to_artifact(guard_on):
    """RC3: "html" in an automation-configuration message is the definition's
    output format — the guard must NOT force create_artifact."""
    assert should_force_create_file(
        _CREATION_MESSAGE,
        tool_calls_for_frontend=[],
        iteration=100,
        max_iterations=10,  # deep in the force window
        automation_setup=True,
    ) is False


def test_normal_file_request_still_forced(guard_on):
    """No regression: a genuine chat file request keeps the forcing."""
    assert should_force_create_file(
        "Please generate an HTML report of last month's sales.",
        tool_calls_for_frontend=[],
        iteration=100,
        max_iterations=10,
        automation_setup=False,
    ) is True


def test_automation_setup_false_keeps_legacy_behavior(guard_on):
    """Default parameter preserves existing call-site behavior."""
    assert should_force_create_file(
        _CREATION_MESSAGE,
        tool_calls_for_frontend=[],
        iteration=100,
        max_iterations=10,
    ) is True


def test_scheduling_tool_call_satisfies_html_request(guard_on):
    """A create_automation with output_format=html still satisfies the guard
    on non-setup turns (existing exemption intact)."""
    assert should_force_create_file(
        "Please generate an HTML report of last month's sales.",
        tool_calls_for_frontend=[
            {"name": "create_automation",
             "arguments_string": '{"output_format": "html"}',
             "results": "ok"},
        ],
        iteration=100,
        max_iterations=10,
        automation_setup=False,
    ) is False


def test_nudge_is_artifact_only_not_automation():
    nudge = build_file_nudge("html")
    assert "create_artifact" in nudge
    assert "automation" not in nudge.lower()
