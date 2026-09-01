"""Tests for failure chat notification + partial output preservation
(Phase 4, Task 3)."""
import os
import sys
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


# -- partial output persistence --------------------------------------------

def test_persist_run_progress_writes_partial_output():
    """_persist_run_progress stores partial text in output_text so a later
    timeout retains whatever the agent produced before it hung."""
    db = MagicMock()
    with patch("app.database.SessionLocal", return_value=db):
        ax._persist_run_progress(
            "exec1", [{"number": 1}], "Working",
            partial_text="partial report so far",
        )
    assert db.execute.called
    # The UPDATE statement must reference output_text.
    stmts = [str(c.args[0]) for c in db.execute.call_args_list if c.args]
    assert any("output_text" in s.lower() for s in stmts)


def test_persist_run_progress_omits_output_text_when_no_partial():
    """When partial_text is None, output_text is NOT touched (don't clobber an
    existing output_text with NULL on a progress-only write)."""
    db = MagicMock()
    with patch("app.database.SessionLocal", return_value=db):
        ax._persist_run_progress("exec1", [{"number": 1}], "Working")
    stmts = [str(c.args[0]) for c in db.execute.call_args_list if c.args]
    assert any("activity_steps" in s.lower() for s in stmts)
    assert not any("output_text" in s.lower() for s in stmts)


# -- _notify_chat_failure ---------------------------------------------------

class _CapturedMsg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_notify_chat_failure_creates_message_with_error_and_partial():
    """_notify_chat_failure writes a ChatMessage containing the error + partial
    output (not a silent failure)."""
    task = MagicMock()
    task.name = "Daily Report"; task.session_id = "sess1"
    task.org_id = "o"; task.app_id = "a"; task.created_by_id = "u"
    execution = MagicMock(); execution.id = "exec1234567890"
    execution.error = "Run timed out after 600s"
    partial = "## Half-finished report\nSome findings..."

    db = MagicMock()
    with patch.object(ax, "ChatMessage", side_effect=lambda **kw: _CapturedMsg(**kw)):
        msg = ax._notify_chat_failure(db, task, execution, partial)

    assert msg is not None
    assert "Daily Report" in msg.content
    assert "timed out" in msg.content.lower()
    assert "Half-finished report" in msg.content
    assert msg.role == "assistant"
    # The failure message must be persisted to a chat session.
    assert db.add.called and db.commit.called


def test_notify_chat_failure_returns_none_when_no_session():
    """If there's no session to notify (and none can be found), return None
    instead of crashing."""
    task = MagicMock()
    task.session_id = None; task.created_by_id = "u"
    execution = MagicMock(); execution.id = "e1"; execution.error = "boom"

    db = MagicMock()
    # No ChatSession found.
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    with patch.object(ax, "ChatMessage", side_effect=lambda **kw: _CapturedMsg(**kw)):
        msg = ax._notify_chat_failure(db, task, execution, "partial")
    assert msg is None


# -- _mark_failed wiring ----------------------------------------------------

def test_mark_failed_notifies_chat_when_enabled():
    """_mark_failed calls _notify_chat_failure when notify_chat is true."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "true"; task.max_retries = "0"
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

    with patch("app.services.automation_dispatcher.schedule_retry", return_value=None) as sr, \
         patch.object(ax, "_notify_chat_failure") as nf:
        ax._mark_failed(db, execution, "boom")
    # No retry (max_retries=0) but failure notification should fire.
    nf.assert_called_once()
    sr.assert_not_called()


def test_mark_failed_skips_notify_when_disabled():
    """_mark_failed does NOT notify when notify_chat is false/off."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "false"; task.max_retries = "0"
    task.session_id = "sess1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0
    execution.automation_task_id = "t1"; execution.status = "running"
    execution.output_text = "partial"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = task

    with patch("app.services.automation_dispatcher.schedule_retry", return_value=None), \
         patch.object(ax, "_notify_chat_failure") as nf:
        ax._mark_failed(db, execution, "boom")
    nf.assert_not_called()


def test_mark_failed_skips_notify_when_retry_scheduled():
    """An intermediate failure (retry scheduled) does NOT notify the chat —
    only the final failure does, avoiding a confusing failed-then-succeeded
    notification pair."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "true"; task.max_retries = "3"
    task.session_id = "sess1"; task.org_id = "o"; task.app_id = "a"
    task.created_by_id = "u"
    execution = MagicMock()
    execution.id = "exec1"; execution.attempt = 0  # 0 < 3 -> retry scheduled
    execution.automation_task_id = "t1"; execution.status = "running"
    execution.output_text = "partial"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    db.query.return_value.filter.return_value.first.return_value = task

    with patch("app.services.automation_dispatcher.schedule_retry", return_value=None) as sr, \
         patch.object(ax, "_notify_chat_failure") as nf:
        ax._mark_failed(db, execution, "transient")
    # Retry scheduled, no failure notification (it's not a final failure yet).
    sr.assert_called_once()
    nf.assert_not_called()


def test_mark_failed_no_retry_notifies_chat_when_enabled():
    """_mark_failed_no_retry (used for pause failures) also notifies chat."""
    task = MagicMock()
    task.id = "t1"; task.notify_chat = "true"; task.session_id = "sess1"
    task.org_id = "o"; task.app_id = "a"; task.created_by_id = "u"
    execution = MagicMock()
    execution.id = "exec1"; execution.automation_task_id = "t1"
    execution.output_text = "partial"

    db = MagicMock()
    rc = MagicMock(); rc.rowcount = 1
    db.execute.return_value = rc
    # _mark_failed_no_retry loads the task via db.query(...).first().
    db.query.return_value.filter.return_value.first.return_value = task

    with patch.object(ax, "_notify_chat_failure") as nf:
        ax._mark_failed_no_retry(db, execution, "paused")
    nf.assert_called_once()
