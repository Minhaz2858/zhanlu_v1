"""Tests for file_turn_guard — generic file-deliverable turn guard.

Covers: intent detection, T-3 forcing, synthesis-boundary nudge/disclose,
flag gating, force_next arming, automation output_format detection.
"""

import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from app.config import settings
from app.services.file_turn_guard import (
    is_file_deliverable_request,
    file_artifact_created,
    should_force_create_file,
    build_file_nudge,
    build_file_disclosure,
    file_turn_guard,
    FileTurnGuardResult,
    FILE_FORCE_WINDOW,
    detect_give_up,
    build_give_up_reprompt,
)


@contextmanager
def _flag(value: bool):
    """Temporarily set settings.FILE_TURN_GUARD_ENABLED."""
    old = getattr(settings, "FILE_TURN_GUARD_ENABLED", False)
    settings.FILE_TURN_GUARD_ENABLED = value
    try:
        yield
    finally:
        settings.FILE_TURN_GUARD_ENABLED = old


# ── is_file_deliverable_request ──────────────────────────────────────────

class TestIsFileDeliverableRequest:
    def test_html_keyword(self):
        ok, fmt = is_file_deliverable_request("create an HTML report")
        assert ok is True
        assert fmt == "html"

    def test_web_page_keyword(self):
        ok, fmt = is_file_deliverable_request("produce a web page summary")
        assert ok is True
        assert fmt == "html"

    def test_docx_keyword(self):
        ok, fmt = is_file_deliverable_request("generate a DOCX document")
        assert ok is True
        assert fmt == "docx"

    def test_xlsx_keyword(self):
        ok, fmt = is_file_deliverable_request("export to xlsx please")
        assert ok is True
        assert fmt == "xlsx"

    def test_pdf_keyword(self):
        ok, fmt = is_file_deliverable_request("make a PDF summary")
        assert ok is True
        assert fmt == "pdf"

    def test_md_keyword(self):
        ok, fmt = is_file_deliverable_request("give me a markdown report")
        assert ok is True
        assert fmt == "md"

    def test_pptx_not_detected(self):
        """PPTX is handled by pptx_turn_guard, not file_turn_guard."""
        ok, fmt = is_file_deliverable_request("create a PowerPoint deck")
        assert ok is False
        assert fmt is None

    def test_no_intent(self):
        ok, fmt = is_file_deliverable_request("hello, how are you?")
        assert ok is False
        assert fmt is None

    def test_none_content(self):
        ok, fmt = is_file_deliverable_request(None)
        assert ok is False
        assert fmt is None

    def test_output_format_html(self):
        """Automation runtime passes output_format explicitly."""
        ok, fmt = is_file_deliverable_request("sync sales data", output_format="html")
        assert ok is True
        assert fmt == "html"

    def test_output_format_docx(self):
        ok, fmt = is_file_deliverable_request("summarize data", output_format="docx")
        assert ok is True
        assert fmt == "docx"

    def test_output_format_pptx_not_covered(self):
        ok, fmt = is_file_deliverable_request("make deck", output_format="pptx")
        assert ok is False

    def test_output_format_overrides_no_intent(self):
        """output_format wins even when user_content has no format keyword."""
        ok, fmt = is_file_deliverable_request("daily sync", output_format="html")
        assert ok is True
        assert fmt == "html"


# ── file_artifact_created ───────────────────────────────────────────────

class TestFileArtifactCreated:
    def test_create_artifact_html(self):
        calls = [{"name": "create_artifact", "arguments_string": "type='html'", "results": ""}]
        assert file_artifact_created(calls, "html") is True

    def test_create_artifact_docx(self):
        calls = [{"name": "create_artifact", "arguments_string": "type='docx'", "results": ""}]
        assert file_artifact_created(calls, "docx") is True

    def test_create_artifact_wrong_format(self):
        calls = [{"name": "create_artifact", "arguments_string": "type='pptx'", "results": ""}]
        assert file_artifact_created(calls, "html") is False

    def test_no_build_tool(self):
        calls = [{"name": "execute_query", "arguments_string": "", "results": ""}]
        assert file_artifact_created(calls, "html") is False

    def test_empty_calls(self):
        assert file_artifact_created([], "html") is False

    def test_none_calls(self):
        assert file_artifact_created(None, "html") is False

    def test_no_target_format_any_match(self):
        """When target_format is None, any _FILE_FORMATS match counts."""
        calls = [{"name": "create_artifact", "arguments_string": "type='xlsx'", "results": ""}]
        assert file_artifact_created(calls) is True

    def test_result_blob_checked(self):
        calls = [{"name": "create_artifact", "arguments_string": "", "results": "created html report"}]
        assert file_artifact_created(calls, "html") is True

    # ── automation scheduling (2026-08-25 regression coverage) ───────

    def test_create_automation_with_html_output_format(self):
        """Daily-Sales-Data-Sync exact reproducer (2026-08-25).

        create_automation args contain output_format='html' → file is
        scheduled for runtime, not produced this turn. Must satisfy
        ``file_artifact_created`` so the disclosure branch in
        file_turn_guard doesn't fire.
        """
        calls = [{
            "name": "AutomationTask.create",
            "arguments_string": (
                '{"name": "Daily Sales Data Sync", "type": "data_sync", '
                '"output_format": "html", "schedule": "0 8 * * *", '
                '"project": "Ecisco BI"}'
            ),
            "results": '{"success": true, "task": {...}}',
        }]
        assert file_artifact_created(calls, "html") is True

    def test_create_automation_other_format_does_not_match(self):
        """automation with output_format=docx must NOT satisfy html search."""
        calls = [{
            "name": "AutomationTask.create",
            "arguments_string": '{"output_format": "docx"}',
            "results": "",
        }]
        assert file_artifact_created(calls, "html") is False
        assert file_artifact_created(calls, "docx") is True

    def test_update_automation_recognised(self):
        calls = [{
            "name": "update_automation",
            "arguments_string": '{"output_format": "xlsx"}',
            "results": "",
        }]
        assert file_artifact_created(calls, "xlsx") is True

    def test_automation_no_output_format_does_not_match(self):
        """automation without output_format → not enough evidence, don't trick the guard."""
        calls = [{
            "name": "create_automation",
            "arguments_string": '{"name": "data sync task", "schedule": "0 * * * *"}',
            "results": "",
        }]
        assert file_artifact_created(calls, "html") is False

    def test_automation_args_via_arguments_dict(self):
        """Field name may be ``arguments`` (dict) instead of ``arguments_string``."""
        import json as _json
        calls = [{
            "name": "create_automation",
            "arguments": _json.dumps({"output_format": "pdf"}),
            "results": "",
        }]
        assert file_artifact_created(calls, "pdf") is True


# ── should_force_create_file ─────────────────────────────────────────────

class TestShouldForceCreateFile:
    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    def test_within_window(self, mock_intent):
        with _flag(True):
            assert should_force_create_file(
                "make HTML report", [],
                iteration=7, max_iterations=10,
                has_artifact_tool=True, dashboard_forced=False, pptx_forced=False,
            ) is True

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    def test_outside_window(self, mock_intent):
        assert should_force_create_file(
            "make HTML report", [],
            iteration=3, max_iterations=10,
            has_artifact_tool=True, dashboard_forced=False, pptx_forced=False,
        ) is False

    def test_flag_off(self):
        with _flag(False):
            assert should_force_create_file(
                "make HTML report", [],
                iteration=7, max_iterations=10,
            ) is False

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    def test_dashboard_forced_blocks(self, mock_intent):
        assert should_force_create_file(
            "make HTML report", [],
            iteration=7, max_iterations=10,
            has_artifact_tool=True, dashboard_forced=True,
        ) is False

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    def test_pptx_forced_blocks(self, mock_intent):
        assert should_force_create_file(
            "make HTML report", [],
            iteration=7, max_iterations=10,
            has_artifact_tool=True, pptx_forced=True,
        ) is False

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=True)
    def test_artifact_already_created(self, mock_created, mock_intent):
        assert should_force_create_file(
            "make HTML report", [],
            iteration=7, max_iterations=10,
            has_artifact_tool=True,
        ) is False

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    def test_no_artifact_tool(self, mock_intent):
        assert should_force_create_file(
            "make HTML report", [],
            iteration=7, max_iterations=10,
            has_artifact_tool=False,
        ) is False


# ── build_file_nudge ────────────────────────────────────────────────────

class TestBuildFileNudge:
    def test_html_nudge(self):
        msg = build_file_nudge("html")
        assert "HTML" in msg
        assert "create_artifact" in msg
        assert "type='html'" in msg

    def test_docx_nudge(self):
        msg = build_file_nudge("docx")
        assert "Word/DOCX" in msg
        assert "type='docx'" in msg

    def test_xlsx_nudge(self):
        msg = build_file_nudge("xlsx")
        assert "Excel" in msg
        assert "type='xlsx'" in msg


# ── build_file_disclosure ───────────────────────────────────────────────

class TestBuildFileDisclosure:
    def test_html_disclosure(self):
        msg = build_file_disclosure("html")
        assert "HTML" in msg
        assert "tool budget" in msg

    def test_docx_disclosure(self):
        msg = build_file_disclosure("docx")
        assert "Word" in msg


# ── file_turn_guard ─────────────────────────────────────────────────────

class TestFileTurnGuard:
    def test_flag_off_returns_none(self):
        with _flag(False):
            result = file_turn_guard("make HTML report", [], budget_remaining=5, attempts=0)
            assert result.action == "none"

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=False)
    def test_nudge_when_budget_sufficient(self, mock_created, mock_intent):
        with _flag(True):
            result = file_turn_guard("make HTML report", [], budget_remaining=3, attempts=0)
            assert result.action == "nudge"
            assert "create_artifact" in result.message
            assert result.detected_format == "html"

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=False)
    def test_disclose_when_budget_low(self, mock_created, mock_intent):
        with _flag(True):
            result = file_turn_guard("make HTML report", [], budget_remaining=1, attempts=0)
            assert result.action == "disclose"
            assert "tool budget" in result.message

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=True)
    def test_none_when_artifact_exists(self, mock_created, mock_intent):
        with _flag(True):
            result = file_turn_guard("make HTML report", [], budget_remaining=5, attempts=0)
            assert result.action == "none"

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(False, None))
    def test_none_when_no_file_intent(self, mock_intent):
        with _flag(True):
            result = file_turn_guard("hello", [], budget_remaining=5, attempts=0)
            assert result.action == "none"

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=False)
    def test_nudge_cap_reached(self, mock_created, mock_intent):
        with _flag(True):
            result = file_turn_guard("make HTML report", [], budget_remaining=5, attempts=2)
            assert result.action == "none"

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=False)
    def test_force_next_on_last_nudge(self, mock_created, mock_intent):
        with _flag(True):
            # attempts=1 (0-indexed), cap=2, so this is the last allowed nudge
            result = file_turn_guard("make HTML report", [], budget_remaining=5, attempts=1)
            assert result.action == "nudge"
            assert result.force_next is True

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=False)
    def test_force_next_false_on_first_nudge(self, mock_created, mock_intent):
        with _flag(True):
            result = file_turn_guard("make HTML report", [], budget_remaining=5, attempts=0)
            assert result.action == "nudge"
            assert result.force_next is False

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "docx"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=False)
    def test_output_format_parameter(self, mock_created, mock_intent):
        with _flag(True):
            result = file_turn_guard(
                "sync data", [],
                budget_remaining=5, attempts=0,
                output_format="docx",
            )
            assert result.action == "nudge"
            assert "docx" in result.message.lower()

    @patch("app.services.file_turn_guard.is_file_deliverable_request",
           return_value=(True, "html"))
    @patch("app.services.file_turn_guard.file_artifact_created", return_value=False)
    def test_automation_daily_sync_scenario(self, mock_created, mock_intent):
        """Reproduce the exact bug: automation Daily Sales Data Sync with html output."""
        with _flag(True):
            # After 2 execute_query calls, budget_remaining=8 → nudge fires
            result = file_turn_guard(
                "Daily Sales Data Sync - output_format: html", [],
                budget_remaining=8, attempts=0,
                output_format="html",
            )
            assert result.action == "nudge"
            assert "create_artifact" in result.message

    # ── integration: scheduled automation must suppress disclosure ─────

    def test_automation_already_scheduled_returns_none(self):
        """THE FIX: create_automation already scheduled the deliverable, so
        file_turn_guard must NOT nudge or disclose.

        Reproducer for the 2026-08-25 second-pass bug: agent called
        create_automation with output_format=html successfully, then the
        guard appended "(The requested HTML report could not be generated
        within this turn's tool budget. Please ask again and I will build
        it.)" even though the user's intent was satisfied by the schedule.
        Now: no nudge, no disclose — clean conversation close.
        """
        scheduled_calls = [{
            "name": "AutomationTask.create",
            "arguments_string": (
                '{"name": "Daily Sales Data Sync", "type": "data_sync", '
                '"output_format": "html", "schedule": "0 8 * * *", '
                '"project": "Ecisco BI"}'
            ),
            "results": '{"success": true, "task": {...}}',
        }]
        with _flag(True):
            result = file_turn_guard(
                user_content="Create daily task with HTML output",
                tool_calls_for_frontend=scheduled_calls,
                budget_remaining=1,  # would normally trigger 'disclose'
                attempts=0,
                output_format="html",
            )
            assert result.action == "none", (
                "Scheduled automation must suppress the file-turn-guard "
                "disclosure; got action=%r message=%r" % (result.action, result.message)
            )
            assert result.message == ""

    def test_automation_scheduled_other_format_does_not_help(self):
        """If user asks for HTML but automation scheduled DOCX, guard still fires."""
        mismatched_calls = [{
            "name": "create_automation",
            "arguments_string": '{"output_format": "docx"}',
            "results": "",
        }]
        with _flag(True):
            result = file_turn_guard(
                user_content="daily report",
                tool_calls_for_frontend=mismatched_calls,
                budget_remaining=1,
                attempts=0,
                output_format="html",
            )
            # File intent was html, automation scheduled docx — guard
            # still has a job, expect nudge or disclose.
            assert result.action in {"nudge", "disclose"}


# ── give-up ("graceful fallback") re-prompt — 2026-08-28 QA finding ─────
# QA trace: agent planned a deck (ask_data_agent x26, run_sandbox_skill x2)
# but ended the turn with "I gathered some information but had trouble
# putting it all together. Could you try again with a more specific
# request?" and NO artifact. The guard must re-prompt instead of letting
# the graceful-fallback close the turn unanswered.

class TestDetectGiveUp:
    def test_trouble_putting(self):
        assert detect_give_up(
            "I gathered some information but had trouble putting it all together."
        ) is True

    def test_couldnt_put_together(self):
        assert detect_give_up("I couldn't put together the deck you asked for.") is True

    def test_try_again_more_specific(self):
        assert detect_give_up("Could you try again with a more specific request?") is True

    def test_normal_delivery_text(self):
        assert detect_give_up("Here is your HTML report with the sales breakdown.") is False

    def test_none_text(self):
        assert detect_give_up(None) is False

    def test_empty_text(self):
        assert detect_give_up("") is False

    # ── precision: negated / generic phrases must NOT fire (2026-08-28) ──

    def test_wont_give_up_not_detected(self):
        """\"I won't give up\" is a commitment to continue, not a give-up."""
        assert detect_give_up("I won't give up — I'll keep working on it.") is False

    def test_will_not_give_up_not_detected(self):
        assert detect_give_up("I will not give up until the report is done.") is False

    def test_never_give_up_not_detected(self):
        assert detect_give_up("I never give up on a deliverable.") is False

    def test_bare_please_try_again_not_detected(self):
        assert detect_give_up("Please try again.") is False

    def test_bare_didnt_work_out_not_detected(self):
        assert detect_give_up("It didn't work out.") is False

    def test_full_qa_reproducer_still_detected(self):
        """The original QA trace must still fire (strong signals present)."""
        assert detect_give_up(
            "I gathered some information but had trouble putting it all together. "
            "Could you try again with a more specific request?"
        ) is True

    def test_direct_give_up_still_detected(self):
        assert detect_give_up("I give up on producing this file.") is True
        assert detect_give_up("I gave up after the third attempt.") is True


class TestBuildGiveUpReprompt:
    def test_contains_create_artifact_and_format(self):
        msg = build_give_up_reprompt("docx")
        assert "create_artifact" in msg
        assert "type='docx'" in msg
        assert "trouble" in msg

    def test_pptx_label(self):
        msg = build_give_up_reprompt("pptx")
        assert "type='pptx'" in msg
        assert "deck" in msg.lower()


class TestGiveUpReprompt:
    def test_qa_deck_trace_reprompts(self):
        """Exact QA reproducer: user asked for a pptx deck, the turn ran
        ask_data_agent x26 + run_sandbox_skill x2, NO pptx artifact was
        created, and the final message is a graceful fallback -> the guard
        must emit its re-prompt (nudge) instead of returning 'none'."""
        tool_calls = [
            {
                "name": "ask_data_agent",
                "arguments_string": '{"query": "c5 c9 market data"}',
                "results": "...",
            }
            for _ in range(26)
        ]
        tool_calls += [
            {
                "name": "run_sandbox_skill",
                "arguments_string": '{"skill": "ppt_skills", "task": "render deck"}',
                "results": "skill output",
            }
            for _ in range(2)
        ]
        with _flag(True):
            result = file_turn_guard(
                user_content="make a c5 c9 market view ppt don't use my data use market data",
                tool_calls_for_frontend=tool_calls,
                budget_remaining=5,
                attempts=0,
                final_assistant_text=(
                    "I gathered some information but had trouble putting it all together. "
                    "Could you try again with a more specific request?"
                ),
            )
            assert result.action == "nudge", (
                "give-up close after a deck request must re-prompt; got %r" % result.action
            )
            assert result.detected_format == "pptx"
            assert "create_artifact" in result.message

    def test_docx_give_up_reprompts(self):
        """DOCX variant — the give-up re-prompt must also fire for
        file_turn_guard's own formats, with the give-up-specific message
        (distinct from the plain synthesis nudge)."""
        tool_calls = [
            {"name": "ask_data_agent", "arguments_string": '{"query": "q1"}', "results": ""},
            {"name": "run_sandbox_skill", "arguments_string": '{"skill": "doc_skill"}', "results": ""},
        ]
        with _flag(True):
            result = file_turn_guard(
                user_content="generate a DOCX document",
                tool_calls_for_frontend=tool_calls,
                budget_remaining=5,
                attempts=0,
                final_assistant_text="I had trouble putting this together. Please try again.",
            )
            assert result.action == "nudge"
            assert result.detected_format == "docx"
            assert "create_artifact" in result.message
            assert "trouble" in result.message  # give-up flavor, not the plain nudge

    def test_give_up_without_file_intent_no_reprompt(self):
        with _flag(True):
            result = file_turn_guard(
                "hello how are you", [],
                budget_remaining=5, attempts=0,
                final_assistant_text="I had trouble with that.",
            )
            assert result.action == "none"

    def test_give_up_with_artifact_no_reprompt(self):
        """Artifact exists this turn -> nothing to re-prompt for."""
        tool_calls = [{"name": "create_artifact", "arguments_string": "type='docx'", "results": ""}]
        with _flag(True):
            result = file_turn_guard(
                "generate a DOCX document", tool_calls,
                budget_remaining=5, attempts=0,
                final_assistant_text="I had trouble putting it together.",
            )
            assert result.action == "none"

    def test_give_up_cap_reused(self):
        """Reuses the existing per-turn nudge cap: attempts >= cap -> none."""
        with _flag(True):
            result = file_turn_guard(
                "generate a DOCX document", [],
                budget_remaining=5, attempts=2,
                final_assistant_text="I had trouble putting it together.",
            )
            assert result.action == "none"

    def test_give_up_low_budget_discloses(self):
        with _flag(True):
            result = file_turn_guard(
                "generate a DOCX document", [],
                budget_remaining=1, attempts=0,
                final_assistant_text="I had trouble putting it together.",
            )
            assert result.action == "disclose"

    def test_pptx_without_give_up_remains_pptx_guard_job(self):
        """No give-up text + pptx request -> still 'none' here: the regular
        pptx nudge/disclose stays with pptx_turn_guard (more specific)."""
        with _flag(True):
            result = file_turn_guard(
                "make a PowerPoint deck", [],
                budget_remaining=5, attempts=0,
                final_assistant_text="I'll draft the deck now.",
            )
            assert result.action == "none"
