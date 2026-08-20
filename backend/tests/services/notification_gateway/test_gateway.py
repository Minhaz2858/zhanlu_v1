"""Tests for the gateway core: gating, parsing, and the send worker."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.notification_gateway.gateway import (
    _send_notification,
    is_valid_email,
    notify_run_finished,
    parse_emails,
)


# ── pure helpers ──────────────────────────────────────────────────────────
def test_parse_emails_empty():
    assert parse_emails(None) == []
    assert parse_emails([]) == []
    assert parse_emails("") == []


def test_parse_emails_filters_and_dedups():
    assert parse_emails(["a@b.com", "A@B.com", "bad", "c@d.com;e@f.com"]) == [
        "a@b.com",
        "c@d.com",
        "e@f.com",
    ]


def test_parse_emails_comma_and_semicolon():
    assert parse_emails("a@b.com, c@d.com; e@f.com") == ["a@b.com", "c@d.com", "e@f.com"]


@pytest.mark.parametrize("addr", ["a@b.com", "x.y+z@sub.domain.co"])
def test_is_valid_email_true(addr):
    assert is_valid_email(addr)


@pytest.mark.parametrize("addr", ["", "nope", "a@b", "@b.com", "a b@c.com"])
def test_is_valid_email_false(addr):
    assert not is_valid_email(addr)


# ── notify_run_finished gating ────────────────────────────────────────────
def _task(emails=None, notify_on="always", attach=True, notify_enabled=True):
    return SimpleNamespace(
        id="t1", name="Task", project="proj",
        notify_enabled=notify_enabled,
        notify_emails=emails, notify_on=notify_on, attach_file=attach,
    )


def _exec():
    return SimpleNamespace(
        id="e1", started_at=None, completed_at=None,
        duration_seconds=None, output_text="", error=None,
    )


def _patch_gateway(monkeypatch, enabled=True):
    from app.config import settings

    monkeypatch.setattr(settings, "NOTIFICATION_GATEWAY_ENABLED", enabled)
    fired = []
    monkeypatch.setattr(
        "app.services.notification_gateway.gateway._fire_and_forget",
        lambda c: fired.append(c),
    )
    return fired


def test_skips_when_disabled(monkeypatch):
    fired = _patch_gateway(monkeypatch, enabled=False)
    notify_run_finished(None, _task(["a@b.com"]), _exec(), [], True)
    assert fired == []


def test_skips_when_no_emails(monkeypatch):
    fired = _patch_gateway(monkeypatch)
    notify_run_finished(None, _task([]), _exec(), [], True)
    assert fired == []


def test_skips_success_when_notify_on_failure(monkeypatch):
    fired = _patch_gateway(monkeypatch)
    notify_run_finished(None, _task(["a@b.com"], "on_failure"), _exec(), [], True)
    assert fired == []


def test_skips_failure_when_notify_on_success(monkeypatch):
    fired = _patch_gateway(monkeypatch)
    notify_run_finished(None, _task(["a@b.com"], "on_success"), _exec(), [], False)
    assert fired == []


def test_fires_on_matching_condition(monkeypatch):
    fired = _patch_gateway(monkeypatch)
    notify_run_finished(None, _task(["a@b.com"], "always"), _exec(), [], True)
    assert len(fired) == 1
    fired[0].close()  # coroutine is intentionally never awaited in this test


def test_skips_when_task_notify_disabled(monkeypatch):
    fired = _patch_gateway(monkeypatch)
    notify_run_finished(
        None, _task(["a@b.com"], "always", notify_enabled=False), _exec(), [], True
    )
    assert fired == []


# ── send worker ───────────────────────────────────────────────────────────
def _payload(**overrides):
    base = dict(
        task_id="t1", task_name="Task", project="p", execution_id="e1",
        started_at=None, finished_at=None, duration_seconds=None,
        summary="", error=None, is_success=True, emails=["a@b.com"],
        attach_file=True, file_ids=[],
    )
    base.update(overrides)
    return base


def _setup_worker(monkeypatch, send_side_effect):
    execution = SimpleNamespace(
        id="e1", activity_steps=[{"number": 1, "description": "x", "status": "done"}],
        output_text="done", error=None, email_notified_at=None,
    )
    session = MagicMock()
    session.get.return_value = execution
    session.query.return_value.filter.return_value.all.return_value = []
    monkeypatch.setattr(
        "app.services.notification_gateway.gateway.SessionLocal",
        MagicMock(return_value=session),
    )
    provider = MagicMock()
    provider.send = AsyncMock(side_effect=send_side_effect)
    monkeypatch.setattr(
        "app.services.notification_gateway.gateway.SmtpProvider", lambda: provider
    )
    return execution, session, provider


@pytest.mark.asyncio
async def test_send_worker_success(monkeypatch):
    execution, session, provider = _setup_worker(monkeypatch, [True])
    await _send_notification(_payload())
    provider.send.assert_called_once()
    assert execution.email_notified_at is not None
    session.commit.assert_called_once()
    assert any(s.get("step_type") == "email_notification" and s["status"] == "done"
               for s in execution.activity_steps)


@pytest.mark.asyncio
async def test_send_worker_retries_transport_then_succeeds(monkeypatch):
    from app.services.notification_gateway.provider import EmailTransportError

    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    execution, session, provider = _setup_worker(
        monkeypatch,
        [EmailTransportError("boom"), EmailTransportError("boom"), True],
    )
    await _send_notification(_payload())
    assert provider.send.call_count == 3
    assert execution.email_notified_at is not None


@pytest.mark.asyncio
async def test_send_worker_fails_fast_on_permanent(monkeypatch):
    from app.services.notification_gateway.provider import EmailPermanentError

    execution, session, provider = _setup_worker(monkeypatch, [EmailPermanentError("refused")])
    await _send_notification(_payload())
    assert provider.send.call_count == 1
    assert execution.email_notified_at is None
    assert any(s.get("step_type") == "email_notification" and s["status"] == "error"
               for s in execution.activity_steps)


@pytest.mark.asyncio
async def test_send_worker_unconfigured_no_retry(monkeypatch):
    execution, session, provider = _setup_worker(monkeypatch, [False])
    await _send_notification(_payload())
    assert provider.send.call_count == 1
    assert execution.email_notified_at is None
