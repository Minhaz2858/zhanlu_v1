"""Integration tests for the email notification gateway hooks in the
automation executor.

Covers:
- ``_mark_failed`` fires ``notify_run_finished`` only on the FINAL failure
  (no retry scheduled), independent of the ``notify_chat`` flag.
- ``_mark_failed_no_retry`` always fires ``notify_run_finished``.
- Email failures are swallowed — they can never break the failure path.
"""
import os
import sys
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


# -- _mark_failed (retry-aware) ---------------------------------------------

def test_mark_failed_final_failure_calls_email_gateway_even_without_chat_notify():
    """Final failure (no retries left) must fire notify_run_finished even when
    notify_chat is off — the email gateway is independent of chat notify."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "false"; task.max_retries = "0"
    task.session_id = "sess1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "t1"
    execution.status = "running"
    execution.output_text = "partial"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = task

    files = [MagicMock()]
    db.query.return_value.filter.return_value.all.return_value = files

    with patch("app.services.automation_dispatcher.schedule_retry") as sr, \
         patch.object(ax, "notify_run_finished") as nr:
        ax._mark_failed(db, execution, "boom")

    sr.assert_not_called()
    nr.assert_called_once()
    args, kwargs = nr.call_args
    assert args[0] is db
    assert args[1] is task
    assert args[2] is execution
    assert args[3] == files
    assert kwargs.get("is_success") is False


def test_mark_failed_intermediate_retry_skips_email_gateway():
    """When a retry is scheduled, no email is sent (only final outcomes)."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "true"; task.max_retries = "3"
    task.session_id = "sess1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "t1"
    execution.status = "running"
    execution.output_text = "partial"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = task

    with patch("app.services.automation_dispatcher.schedule_retry") as sr, \
         patch.object(ax, "notify_run_finished") as nr:
        ax._mark_failed(db, execution, "transient boom")

    sr.assert_called_once()
    nr.assert_not_called()


def test_mark_failed_swallows_email_gateway_exception():
    """An exception inside notify_run_finished must never break _mark_failed."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "false"; task.max_retries = "0"
    task.session_id = "sess1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "t1"
    execution.status = "running"
    execution.output_text = "partial"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = task

    with patch("app.services.automation_dispatcher.schedule_retry") as sr, \
         patch.object(ax, "notify_run_finished", side_effect=RuntimeError("smtp down")):
        ax._mark_failed(db, execution, "boom")  # must not raise

    sr.assert_not_called()


# -- _mark_failed_no_retry (paused/confirmation failures) -------------------

def test_mark_failed_no_retry_calls_email_gateway():
    """_mark_failed_no_retry fires notify_run_finished with is_success=False."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "false"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "t1"
    execution.status = "running"
    execution.output_text = "paused for confirmation"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = task

    files = [MagicMock()]
    db.query.return_value.filter.return_value.all.return_value = files

    with patch.object(ax, "notify_run_finished") as nr:
        ax._mark_failed_no_retry(db, execution, "paused")

    nr.assert_called_once()
    args, kwargs = nr.call_args
    assert args[0] is db
    assert args[1] is task
    assert args[2] is execution
    assert args[3] == files
    assert kwargs.get("is_success") is False


def test_mark_failed_no_retry_skips_email_when_task_missing():
    """No task -> no email gateway call (nothing to notify)."""
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "missing"
    execution.status = "running"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = None

    with patch.object(ax, "notify_run_finished") as nr:
        ax._mark_failed_no_retry(db, execution, "paused")

    nr.assert_not_called()


def test_mark_failed_no_retry_swallows_email_gateway_exception():
    """An exception inside notify_run_finished must never break
    _mark_failed_no_retry."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "false"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "t1"
    execution.status = "running"
    execution.output_text = "paused"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = task

    with patch.object(ax, "notify_run_finished", side_effect=RuntimeError("boom")):
        ax._mark_failed_no_retry(db, execution, "paused")  # must not raise
