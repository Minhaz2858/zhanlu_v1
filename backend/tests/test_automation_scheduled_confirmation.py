"""Regression tests for the 2026-08-25 fourth-pass bug.

The Daily Sales Data Sync user retried after the third-pass fix
(route-on-intent to ``automation_agent``). The agent now correctly:
- routed to ``automation_agent``,
- called ``list_knowledge_bases``,
- called ``AutomationTask.create`` successfully (succeeded in 0.1s per the
  activity rail),
- emitted EMPTY assistant content (no final prose).

But the bubble still showed ``_GENERIC_EMPTY_CONTENT_FALLBACK``
("I gathered some information but had trouble putting it all
together…"), because the auto-rebind did its job but the v3 loop's
empty-content path STILL fell through to the generic apology when
``is_file_deliverable_request`` matched ``html`` AND
``file_artifact_created`` reported True (which is now automation-
aware — see file_turn_guard).

The fix is in ``_choose_fallback`` in
``backend/app/routers/agents.py``: the branch returns
``_automation_scheduled_confirmation(tool_calls_for_frontend, _fmt)``
instead of falling through to the generic apology. The helper
extracts the task name + schedule + project from the
``create_automation`` args blob and renders a deterministic
confirmation pointing at the next cron fire.

These tests pin:
- The new helper is reachable from ``_choose_fallback``.
- The helper extracts ``name`` / ``schedule`` / ``project`` correctly
  from both ``arguments_string`` (string) and ``arguments`` (dict)
  shapes.
- The helper falls back to a sane message even when the args blob
  is missing or malformed.
- Realistic Dashboard Daily Sales Data Sync-style fixtures produce
  the exact confirmation text expected.
- Backward-compat: the file still does NOT recommend appending the
  generic "could not be generated" disclosure.
"""

from __future__ import annotations

import os

# Override UPLOAD_DIR before any app.* import (matches sibling tests).
os.environ.setdefault("UPLOAD_DIR", "/tmp/test_uploads_automation_scheduled")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)

import json

from app.routers.agents import (
    _automation_scheduled_confirmation,
    _choose_fallback,
)


# ── helper extraction: happy paths ──────────────────────────────────────


def test_extracts_name_schedule_project_from_arguments_string():
    """Reproducer of the 2026-08-25 Daily Sales Data Sync task.

    The create_automation arguments_string was the canonical composer
    template. The helper must surface ``name``, ``schedule`` and
    ``project`` so the user sees a precise confirmation.
    """
    calls = [
        {
            "name": "AutomationTask.create",
            "arguments_string": json.dumps(
                {
                    "name": "Daily Sales Data Sync",
                    "type": "data_sync",
                    "schedule": "0 8 * * *",
                    "output_format": "html",
                    "project": "Ecisco BI",
                }
            ),
            "results": '{"success": true, "task": {...}}',
        },
    ]
    msg = _automation_scheduled_confirmation(calls, "html")
    assert "Daily Sales Data Sync" in msg
    assert "0 8 * * *" in msg
    assert "Ecisco BI" in msg
    assert "HTML" in msg  # format label uppercase
    assert "cron" in msg.lower()


def test_extracts_from_arguments_dict_shape():
    """Some call sites use ``arguments`` as a dict instead of a JSON string."""
    calls = [
        {
            "name": "create_automation",
            "arguments": {
                "name": "Inventory snapshot",
                "schedule": "every morning",
                "project": "Logistics",
                "output_format": "pdf",
            },
            "results": "",
        },
    ]
    msg = _automation_scheduled_confirmation(calls, "pdf")
    assert "Inventory snapshot" in msg
    assert "every morning" in msg
    assert "Logistics" in msg
    assert "PDF" in msg


def test_extracts_from_update_automation():
    """Update calls also count as scheduling (the runtime picks up the
    new config at the next fire)."""
    calls = [
        {
            "name": "update_automation",
            "arguments_string": json.dumps(
                {
                    "name": "Weekly digest",
                    "schedule": "0 9 * * 1",
                    "project": "Exec Dashboard",
                    "output_format": "docx",
                }
            ),
            "results": "",
        },
    ]
    msg = _automation_scheduled_confirmation(calls, "docx")
    assert "Weekly digest" in msg
    assert "0 9 * * 1" in msg
    assert "Exec Dashboard" in msg
    assert "DOCX" in msg


# ── helper extraction: edge cases ──────────────────────────────────────


def test_ignores_unrelated_tool_calls():
    """Only ``_AUTOMATION_SCHEDULING_TOOL_NAMES`` shape matters."""
    calls = [
        {
            "name": "list_knowledge_bases",
            "arguments_string": "{}",
            "results": "{...}",
        },
        {
            "name": "create_automation",
            "arguments_string": json.dumps(
                {
                    "name": "Forecast Run",
                    "schedule": "0 0 * * *",
                    "project": "EDIA",
                    "output_format": "md",
                }
            ),
            "results": "",
        },
    ]
    msg = _automation_scheduled_confirmation(calls, "md")
    assert "Forecast Run" in msg
    assert "EDIA" in msg
    assert "MD" in msg


def test_falls_back_to_default_text_when_no_data():
    """With no matching tool calls, the helper should still produce a
    deterministic confirmation (so we never return empty / None and
    fall through to the generic apology).
    """
    calls = [
        {"name": "list_knowledge_bases", "arguments_string": "{}"},
    ]
    msg = _automation_scheduled_confirmation(calls, "html")
    assert msg, "must return non-empty text"
    assert "automatically by the runtime" in msg.lower()
    assert "HTML" in msg


def test_tolerates_malformed_arguments():
    """Even with garbage args, we produce a deterministic message."""
    calls = [
        {"name": "create_automation", "arguments_string": "{not json"},
        {"name": "create_automation", "arguments_string": None},
        {"name": "create_automation", "arguments_string": "   "},
    ]
    for c in calls:
        msg = _automation_scheduled_confirmation([c], "xlsx")
        assert msg
        assert "XLSX" in msg


def test_handles_empty_tool_calls_list():
    msg = _automation_scheduled_confirmation([], "html")
    assert msg
    assert "HTML" in msg


def test_handles_none_tool_calls():
    """``None`` is treated as empty list (defensive)."""
    msg = _automation_scheduled_confirmation(None, "html")
    assert msg
    assert "HTML" in msg


# ── integration with _choose_fallback ─────────────────────────────────


def test_choose_fallback_returns_automation_confirmation_when_scheduled():
    """The full integration: with create_automation scheduled, the
    ``_choose_fallback`` returns ``_automation_scheduled_confirmation``
    text (NOT the generic apology, NOT the disclosure).
    """
    calls = [
        {
            "name": "AutomationTask.create",
            "arguments_string": json.dumps(
                {
                    "name": "Daily Sales Data Sync",
                    "schedule": "0 8 * * *",
                    "project": "Ecisco BI",
                    "output_format": "html",
                }
            ),
            "results": "",
        },
    ]
    user_content = (
        "Create a new Automation Task:\n"
        "- Name: Daily Sales Data Sync\n"
        "- Type: Data Sync\n"
        "- Schedule: Daily 08:00\n"
        "- Output format: HTML report (html)\n"
        "- Project: Ecisco BI"
    )
    result = _choose_fallback(calls, orch_created=[], user_content=user_content)
    assert "Daily Sales Data Sync" in result
    assert "0 8 * * *" in result
    assert "Ecisco BI" in result
    # NO generic apology.
    assert "had trouble putting it all together" not in result
    # NO misleading budget disclosure.
    assert "could not be generated" not in result.lower()


def test_choose_fallback_falls_back_to_disclosure_when_not_scheduled():
    """When the user asks for HTML but NO automation was scheduled
    (e.g. a real ``Create me an HTML report`` request), the disclosure
    still fires correctly. Pin the inverse of the happy path.
    """
    calls = [
        {"name": "list_knowledge_bases", "arguments_string": "{}"},
    ]
    user_content = "Generate an HTML report of Q2 sales"
    result = _choose_fallback(calls, orch_created=[], user_content=user_content)
    assert "could not be generated" in result.lower(), result


def test_choose_fallback_falls_through_to_generic_when_no_file_intent():
    """When the user message has no file-format intent AND no artifacts
    were produced, ``_GENERIC_EMPTY_CONTENT_FALLBACK`` fires (existing
    behaviour, must not regress).
    """
    calls = [
        {"name": "create_automation", "arguments_string": "{}"},
    ]
    user_content = "Hello, what can you do?"
    result = _choose_fallback(calls, orch_created=[], user_content=user_content)
    assert "had trouble putting it all together" in result.lower(), result
