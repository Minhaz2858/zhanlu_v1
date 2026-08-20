"""Tests for the SMTP email provider (no real network)."""

import smtplib

import pytest

from app.services.notification_gateway.provider import (
    EmailPermanentError,
    EmailTransportError,
    SmtpProvider,
    _build_message,
)


def test_build_message_no_attachments():
    msg = _build_message("from@x.com", ["to@x.com"], "Subj", "<p>hi</p>", "hi", [])
    assert msg["Subject"] == "Subj"
    assert msg["To"] == "to@x.com"
    assert msg.get_content_type() == "multipart/alternative"


def test_build_message_with_attachment():
    att = {"filename": "report.pdf", "content": b"%PDF", "mime": "application/pdf"}
    msg = _build_message("from@x.com", ["to@x.com"], "Subj", "<p>hi</p>", "hi", [att])
    assert msg.get_content_type() == "multipart/mixed"
    parts = list(msg.walk())
    assert any(p.get_content_type() == "application/pdf" for p in parts)


def _configured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USER", "user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(settings, "SMTP_FROM", "from@x.com")
    monkeypatch.setattr(settings, "SMTP_USE_SSL", True)


@pytest.mark.asyncio
async def test_send_returns_false_when_unconfigured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SMTP_HOST", "")
    result = await SmtpProvider().send(["a@b.com"], "s", "<p>h</p>", "h", [])
    assert result is False


@pytest.mark.asyncio
async def test_send_ssl_success(monkeypatch):
    _configured(monkeypatch)
    sent = {}

    class FakeServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg, from_addr, to_addrs):
            sent["to"] = list(to_addrs)

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeServer)
    result = await SmtpProvider().send(["a@b.com"], "s", "<p>h</p>", "h", [])
    assert result is True
    assert sent["login"] == ("user", "secret")
    assert sent["to"] == ["a@b.com"]


@pytest.mark.asyncio
async def test_send_auth_error_is_permanent(monkeypatch):
    _configured(monkeypatch)

    class FakeServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad")

        def send_message(self, msg, from_addr, to_addrs):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeServer)
    with pytest.raises(EmailPermanentError):
        await SmtpProvider().send(["a@b.com"], "s", "<p>h</p>", "h", [])


@pytest.mark.asyncio
async def test_send_connect_error_is_transport(monkeypatch):
    _configured(monkeypatch)

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    with pytest.raises(EmailTransportError):
        await SmtpProvider().send(["a@b.com"], "s", "<p>h</p>", "h", [])
