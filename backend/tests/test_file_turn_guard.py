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
