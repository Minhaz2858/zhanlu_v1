"""Email Notification Gateway — fire-and-forget run result emails.

``notify_run_finished`` is the single entry point, called from the automation
executor at the three success/failure hook points. It gates on
``NOTIFICATION_GATEWAY_ENABLED`` + per-task ``notify_emails`` / ``notify_on``
and then dispatches a background worker thread (``asyncio.run`` on its own
loop) so an SMTP call can never block or delay the run.

The worker opens its own ``SessionLocal()`` (caller sessions are not safe to
share across threads), builds the email, retries transient SMTP errors with
backoff, and appends the outcome to ``AutomationExecution.activity_steps`` so
it shows up in the Node Execution Logs. Email failures NEVER mark the run as
failed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database import SessionLocal
from app.models.automation_execution import AutomationExecution
from app.models.automation_file import AutomationFile
from app.models.automation_task import AutomationTask

from .download_link import build_download_url
from .provider import EmailPermanentError, EmailTransportError, SmtpProvider
from .templates import EmailContext, build_email_html, build_email_subject, build_email_text

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_VALID_NOTIFY_ON = {"always", "on_success", "on_failure"}
_BACKOFF_SECONDS = (1, 2, 4)


def is_valid_email(addr: str) -> bool:
    return bool(addr) and bool(_EMAIL_RE.match(addr.strip()))


def parse_emails(raw: Any) -> list[str]:
    """Leniently parse ``notify_emails`` into a de-duplicated list of valid
    addresses. Invalid entries are silently dropped (gateway path)."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in str(item).replace(";", ",").split(","):
            addr = part.strip().lower()
            if addr and addr not in seen and is_valid_email(addr):
                seen.add(addr)
                result.append(addr)
    return result


def notify_run_finished(
    db: Any,
    task: AutomationTask,
    execution: AutomationExecution,
    files: list[AutomationFile] | None,
    is_success: bool,
) -> None:
    """Gate and dispatch the run-finished email. Never raises, never blocks.

    ``db`` is accepted for call-site symmetry with the executor's ``_notify_chat``
    hooks but is intentionally unused — the worker re-opens its own session.
    """
    del db  # caller session is not shared across threads
    if not settings.NOTIFICATION_GATEWAY_ENABLED:
        return
    # Per-task master switch: only an explicit ``notify_enabled=True`` sends
    # email (default False), independent of whether recipients are configured.
    if not bool(getattr(task, "notify_enabled", False)):
        return
    emails = parse_emails(getattr(task, "notify_emails", None))
    if not emails:
        logger.info("email notification skipped: no recipients for task %s", task.id)
        return
    notify_on = (getattr(task, "notify_on", None) or "always").strip().lower()
    if notify_on not in _VALID_NOTIFY_ON:
        notify_on = "always"
    if notify_on == "on_success" and not is_success:
        return
    if notify_on == "on_failure" and is_success:
        return

    payload = {
        "task_id": task.id,
        "task_name": task.name or "Automation Task",
        "project": getattr(task, "project", None) or "",
        "execution_id": execution.id,
        "started_at": _iso(execution.started_at),
        "finished_at": _iso(execution.completed_at),
        "duration_seconds": execution.duration_seconds,
        "summary": (execution.output_text or "")[:4000],
        "error": execution.error,
        "is_success": bool(is_success),
        "emails": emails,
        "attach_file": _coerce_bool(getattr(task, "attach_file", None)),
        "file_ids": [f.id for f in (files or []) if getattr(f, "id", None)],
    }
    _fire_and_forget(_send_notification(payload))


def _fire_and_forget(coro: Any) -> None:
    def _runner() -> None:
        try:
            asyncio.run(coro)
        except Exception:
            logger.exception("email notification worker crashed")

    threading.Thread(
        target=_runner, name="email-notification-gateway", daemon=True
    ).start()


async def _send_notification(payload: dict) -> None:
    pdb = SessionLocal()
    try:
        execution_id = payload["execution_id"]
        execution = pdb.get(AutomationExecution, execution_id)
        if execution is None:
            logger.warning("email notification worker: execution %s not found", execution_id)
            return

        files = (
            pdb.query(AutomationFile)
            .filter(AutomationFile.execution_id == execution_id)
            .all()
        )
        files = [f for f in files if f.id in payload["file_ids"]]

        attachments: list[dict] = []
        file_note = ""
        download_url: str | None = None
        if not files:
            file_note = "No output file was produced for this run."
        elif payload["attach_file"]:
            chosen = _pick_attachable(files)
            if chosen is not None:
                content = _read_bytes(chosen.file_path)
                if content is not None:
                    attachments.append(
                        {
                            "filename": chosen.name or "output",
                            "content": content,
                            "mime": chosen.mime_type or "application/octet-stream",
                        }
                    )
                    file_note = f"Attached: {chosen.name}"
            if not attachments:
                download_url = build_download_url(files[0].id)
                file_note = "Output file available via download link (too large to attach)."
        else:
            download_url = build_download_url(files[0].id)
            file_note = "Output file available via download link."

        ctx = EmailContext(
            task_name=payload["task_name"],
            project=payload["project"],
            is_success=payload["is_success"],
            started_at=_parse_dt(payload.get("started_at")),
            finished_at=_parse_dt(payload.get("finished_at")),
            duration_seconds=_coerce_int(payload.get("duration_seconds")),
            summary=payload.get("summary") or "",
            error=payload.get("error"),
            step_summary=_summarize_steps(execution.activity_steps),
            file_note=file_note,
            download_url=download_url,
        )
        subject = build_email_subject(ctx)
        html = build_email_html(ctx)
        text = build_email_text(ctx)

        provider = SmtpProvider()
        emails = payload["emails"]
        success = False
        last_error = ""
        max_retries = max(1, int(settings.EMAIL_MAX_RETRIES))
        for attempt in range(max_retries):
            try:
                sent = await provider.send(emails, subject, html, text, attachments)
                if sent:
                    success = True
                    break
                last_error = "SMTP not configured"
                break  # not configured → no point retrying
            except EmailPermanentError as exc:
                last_error = str(exc)
                break
            except EmailTransportError as exc:
                last_error = str(exc)
                if attempt < max_retries - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])

        _append_email_step(execution, success, emails, last_error)
        if success:
            execution.email_notified_at = datetime.now(timezone.utc)
        pdb.commit()
        if success:
            logger.info("email notification sent for execution %s to %s", execution_id, emails)
        else:
            logger.warning("email notification failed for execution %s: %s", execution_id, last_error)
    except Exception:
        logger.exception("email notification worker crashed for execution %s", payload.get("execution_id"))
        try:
            pdb.rollback()
        except Exception:
            pass
    finally:
        pdb.close()


def _pick_attachable(files: list[AutomationFile]) -> AutomationFile | None:
    cap = int(settings.EMAIL_ATTACH_MAX_BYTES)
    for f in files:
        if f.file_path and (f.size is None or f.size <= cap):
            return f
    return None


def _read_bytes(path: str | None) -> bytes | None:
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        logger.warning("email notification: could not read file %s", path)
        return None


def _append_email_step(
    execution: AutomationExecution, success: bool, emails: list[str], error: str
) -> None:
    steps = list(execution.activity_steps or [])
    next_num = 1
    for s in steps:
        if isinstance(s, dict) and isinstance(s.get("number"), int):
            next_num = max(next_num, s["number"] + 1)
    if success:
        description = f"Email notification sent to {', '.join(emails)}"
        status = "done"
    else:
        description = f"Email notification failed: {error or 'unknown error'}"
        status = "error"
    steps.append(
        {
            "number": next_num,
            "description": description,
            "status": status,
            "step_type": "email_notification",
        }
    )
    execution.activity_steps = steps


def _summarize_steps(steps: list | None) -> list[str]:
    out: list[str] = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        desc = str(s.get("description") or "").strip()
        if not desc:
            continue
        status = str(s.get("status") or "")
        out.append(f"{desc} ({status})" if status else desc)
    return out[-8:]  # keep the last 8 steps so the email stays concise


def _coerce_bool(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
