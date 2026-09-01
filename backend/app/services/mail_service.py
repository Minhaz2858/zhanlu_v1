"""Mail service — send real emails (OTP codes, password resets) via SMTP.

Uses the stdlib ``smtplib`` (no new dependency) driven off the event loop via
``asyncio.to_thread`` so an SMTP round-trip never blocks the FastAPI loop.

Behaviour:
  * If ``SMTP_HOST`` is configured → actually send the email.
  * Otherwise → fall back to logging the message (the historical dev
    behaviour) so local dev keeps working without any SMTP account.

All send functions are best-effort: they log and return ``False`` on failure
instead of raising, so an SMTP outage never breaks the auth flow.
"""

import asyncio
import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from app.config import settings

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


def _from_address() -> str:
    addr = settings.SMTP_FROM or settings.SMTP_USER
    return formataddr((str(Header(settings.SMTP_FROM_NAME, "utf-8")), addr))


def _send_sync(to_addr: str, subject: str, html_body: str, text_body: str) -> None:
    """Blocking SMTP send (runs in a worker thread)."""
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = _from_address()
    msg["To"] = to_addr

    if settings.SMTP_USE_SSL:
        server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
    else:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
    try:
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(_from_address(), [to_addr], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_email(to_addr: str, subject: str, html_body: str, text_body: str = "") -> bool:
    """Send an email. Returns True on success, False on failure/not-configured.

    Falls back to logging the message when SMTP is not configured (dev mode).
    """
    if not text_body:
        # Strip tags crudely for a plaintext alternative.
        import re
        text_body = re.sub(r"<[^>]+>", "", html_body)

    if not _smtp_configured():
        logger.warning(
            "MAIL (dev, no SMTP configured) → to=%s subject=%s\n%s",
            to_addr, subject, text_body[:500],
        )
        return False

    try:
        await asyncio.to_thread(_send_sync, to_addr, subject, html_body, text_body)
        logger.info("MAIL sent → to=%s subject=%s", to_addr, subject)
        return True
    except Exception as e:  # noqa: BLE001 — never break auth flow on SMTP errors
        logger.error("MAIL send failed → to=%s: %s", to_addr, e)
        return False


async def send_otp_email(to_addr: str, otp: str, purpose: str = "verify your account") -> bool:
    """Send the one-time verification code email.

    Returns True if the email was actually sent. The caller should still log
    the OTP server-side as a fallback (so dev/no-SMTP still works).
    """
    subject = f"Your Zhanlu verification code: {otp}"
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;padding:24px">
      <h2 style="color:#b45309;margin:0 0 8px">Zhanlu System · Synexia</h2>
      <p style="color:#444;font-size:15px;line-height:1.6">
        Use this code to {purpose}. It expires in <b>10 minutes</b>.
      </p>
      <div style="margin:24px 0;text-align:center">
        <span style="display:inline-block;font-size:34px;letter-spacing:8px;font-weight:700;
                     color:#111;background:#fdf3e7;border:1px solid #f5c98a;border-radius:10px;
                     padding:14px 26px">{otp}</span>
      </div>
      <p style="color:#888;font-size:12px;line-height:1.5">
        If you didn't request this code, you can ignore this email.
      </p>
    </div>
    """
    text = f"Your Zhanlu verification code is {otp} (expires in 10 minutes). Use it to {purpose}."
    return await send_email(to_addr, subject, html, text)


__all__ = ["send_email", "send_otp_email"]
