"""Email delivery abstraction.

``EmailProvider`` is the stable interface the gateway talks to; ``SmtpProvider``
is the built-in implementation backed by the existing ``SMTP_*`` settings (SSL
on 465 / STARTTLS on 587, driven by ``SMTP_USE_SSL``). Future providers
(SendGrid, Aliyun DirectMail, SES) implement the same ``send`` method and drop
in behind the same interface.

Error model:
  - ``send`` returns ``True`` on success.
  - ``EmailTransportError`` — transient (connect/timeout); the gateway retries.
  - ``EmailPermanentError`` — auth / invalid-recipient; the gateway fails fast.
  - ``send`` returns ``False`` when SMTP is not configured (dev mode); the
    gateway logs and does not retry.

Credentials are read from ``settings`` at send time and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import socket
import ssl
from abc import ABC, abstractmethod
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from app.config import settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """Base class for email delivery failures."""

    permanent = False


class EmailTransportError(EmailDeliveryError):
    """Transient SMTP transport failure — safe to retry with backoff."""


class EmailPermanentError(EmailDeliveryError):
    """Permanent failure (auth / invalid recipient) — do not retry."""

    permanent = True


class EmailProvider(ABC):
    """Stable email-delivery interface consumed by the gateway."""

    @abstractmethod
    async def send(
        self,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str,
        attachments: list[dict] | None = None,
    ) -> bool:
        """Deliver one email.

        ``attachments`` entries: ``{"filename": str, "content": bytes, "mime": str}``.
        Returns ``True`` on success, ``False`` when the transport is unconfigured.
        Raises ``EmailTransportError`` / ``EmailPermanentError`` on failure.
        """
        raise NotImplementedError


class SmtpProvider(EmailProvider):
    """SMTP implementation over the existing ``SMTP_*`` settings."""

    async def send(
        self,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str,
        attachments: list[dict] | None = None,
    ) -> bool:
        if not _smtp_configured():
            logger.info("SMTP not configured; email send skipped")
            return False
        return await asyncio.to_thread(
            _send_smtp, to, subject, html_body, text_body, attachments or []
        )


def _smtp_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


def _build_message(
    from_addr: str,
    to: list[str],
    subject: str,
    html_body: str,
    text_body: str,
    attachments: list[dict],
) -> MIMEMultipart:
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(alt)
        for att in attachments:
            content = att.get("content") or b""
            mime = att.get("mime") or "application/octet-stream"
            subtype = mime.split("/")[-1] if "/" in mime else "octet-stream"
            part = MIMEApplication(content, _subtype=subtype)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", att.get("filename") or "output"),
            )
            msg.attach(part)
    else:
        msg = alt

    msg["Subject"] = subject
    msg["From"] = formataddr((settings.SMTP_FROM_NAME or "Zhanlu System", from_addr))
    msg["To"] = ", ".join(to)
    return msg


def _send_smtp(
    to: list[str],
    subject: str,
    html_body: str,
    text_body: str,
    attachments: list[dict],
) -> bool:
    from_addr = settings.SMTP_FROM or settings.SMTP_USER
    msg = _build_message(from_addr, to, subject, html_body, text_body, attachments)
    port = settings.SMTP_PORT or (465 if settings.SMTP_USE_SSL else 587)
    context = ssl.create_default_context()
    try:
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, port, timeout=30)
        with server:
            if not settings.SMTP_USE_SSL:
                server.starttls(context=context)
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg, from_addr=from_addr, to_addrs=list(to))
        return True
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailPermanentError("SMTP authentication failed") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise EmailPermanentError("one or more recipients were refused") from exc
    except (smtplib.SMTPException, socket.error, OSError, TimeoutError) as exc:
        raise EmailTransportError(f"SMTP transport error: {exc}") from exc
