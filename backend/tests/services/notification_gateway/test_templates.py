"""Tests for email content builders."""

from datetime import datetime, timezone

from app.services.notification_gateway.templates import (
    EmailContext,
    build_email_html,
    build_email_subject,
    build_email_text,
)


def _ctx(**overrides):
    base = dict(
        task_name="Daily Sales Sync",
        project="Operations",
        is_success=True,
        started_at=datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 20, 8, 2, tzinfo=timezone.utc),
        duration_seconds=125,
        summary="Synced 42 rows.",
        error=None,
        step_summary=["Query ERP (done)", "Write report (done)"],
        file_note="Attached: report.xlsx",
        download_url=None,
    )
    base.update(overrides)
    return EmailContext(**base)


def test_subject_success_includes_duration():
    subject = build_email_subject(_ctx())
    assert subject.startswith("✅ Daily Sales Sync")
    assert "completed" in subject
    assert "2m 5s" in subject


def test_subject_failure():
    subject = build_email_subject(_ctx(is_success=False, error="boom"))
    assert subject.startswith("❌ Daily Sales Sync")
    assert "FAILED" in subject


def test_html_escapes_user_content():
    html = build_email_html(_ctx(task_name="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_has_status_badge():
    assert "Completed" in build_email_html(_ctx())
    assert "Failed" in build_email_html(_ctx(is_success=False))


def test_text_includes_summary_and_error():
    text = build_email_text(_ctx())
    assert "Synced 42 rows." in text
    assert "Attached: report.xlsx" in text

    fail = build_email_text(_ctx(is_success=False, error="connection reset"))
    assert "Error:" in fail
    assert "connection reset" in fail


def test_html_renders_markdown_summary():
    html = build_email_html(_ctx(
        summary="## Headline\n\n**bold** text\n\n| A | B |\n|---|---|\n| 1 | 2 |",
    ))
    assert "<h2" in html
    assert "<strong>bold</strong>" in html
    assert "<table" in html
    assert "<th" in html


def test_html_escapes_raw_html_in_summary():
    html = build_email_html(_ctx(summary="<script>alert(1)</script> safe"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_text_strips_markdown_markers():
    text = build_email_text(_ctx(summary="## Head\n\n**bold** and `code`\n\n- item"))
    assert "## Head" not in text
    assert "**bold**" not in text
    assert "bold" in text
    assert "code" in text
    assert "item" in text
