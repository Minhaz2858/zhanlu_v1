"""Email content builders for automation run notifications.

Table-based, inline-CSS HTML so the message renders consistently in Gmail and
Outlook (both strip ``<style>`` blocks). A plain-text fallback mirrors the HTML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape


@dataclass
class EmailContext:
    task_name: str
    project: str
    is_success: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None
    summary: str = ""
    error: str | None = None
    step_summary: list[str] = field(default_factory=list)
    file_note: str = ""
    download_url: str | None = None


def build_email_subject(ctx: EmailContext) -> str:
    ts = _fmt_dt(ctx.finished_at)
    if ctx.is_success:
        dur = f" ({_fmt_duration(ctx.duration_seconds)})" if ctx.duration_seconds is not None else ""
        return f"✅ {ctx.task_name} — completed {ts}{dur}"
    return f"❌ {ctx.task_name} — FAILED {ts}"


def build_email_text(ctx: EmailContext) -> str:
    status = "Completed" if ctx.is_success else "Failed"
    lines = [
        f"{ctx.task_name}",
        f"Project: {ctx.project or '—'}",
        f"Status: {status}",
    ]
    if ctx.started_at is not None:
        lines.append(f"Started: {_fmt_dt(ctx.started_at)}")
    if ctx.duration_seconds is not None:
        lines.append(f"Duration: {_fmt_duration(ctx.duration_seconds)}")
    if ctx.step_summary:
        lines.append("")
        lines.append("Steps:")
        lines.extend(f"  • {s}" for s in ctx.step_summary)
    if ctx.is_success and ctx.summary:
        lines.append("")
        lines.append("Summary:")
        lines.append(_strip_markdown(ctx.summary))
    if ctx.file_note:
        lines.append("")
        lines.append(ctx.file_note)
        if ctx.download_url:
            lines.append(ctx.download_url)
    if not ctx.is_success and ctx.error:
        lines.append("")
        lines.append("Error:")
        lines.append(ctx.error)
    lines.append("")
    lines.append("This email was generated automatically by the Zhanlu automation system.")
    return "\n".join(lines)


def build_email_html(ctx: EmailContext) -> str:
    status_label = "Completed" if ctx.is_success else "Failed"
    status_color = "#16A34A" if ctx.is_success else "#DC2626"
    status_bg = "#ECFDF5" if ctx.is_success else "#FEF2F2"

    rows: list[str] = []
    rows.append(_row("Task", _b(ctx.task_name)))
    rows.append(_row("Project", _e(ctx.project or "—")))
    rows.append(_row("Status", _badge(status_label, status_color, status_bg)))
    if ctx.started_at is not None:
        rows.append(_row("Started", _e(_fmt_dt(ctx.started_at))))
    if ctx.duration_seconds is not None:
        rows.append(_row("Duration", _e(_fmt_duration(ctx.duration_seconds))))

    blocks: list[str] = [_table(rows)]

    if ctx.step_summary:
        steps = "".join(f"<li style='margin:4px 0;'>{_e(s)}</li>" for s in ctx.step_summary)
        blocks.append(
            f"<h3 style='margin:20px 0 6px;font-size:14px;color:#334155;'>Steps</h3>"
            f"<ol style='margin:0 0 0 18px;padding:0;color:#475569;font-size:13px;'>{steps}</ol>"
        )

    if ctx.is_success and ctx.summary:
        blocks.append(
            f"<h3 style='margin:20px 0 6px;font-size:14px;color:#334155;'>Summary</h3>"
            f"<div style='color:#334155;font-size:13px;line-height:1.6;'>{_render_markdown(ctx.summary)}</div>"
        )

    if ctx.file_note:
        link_html = ""
        if ctx.download_url:
            link_html = (
                f"<br/><a href='{_e(ctx.download_url)}' "
                f"style='color:#4F46E5;font-weight:600;'>Download output file</a>"
            )
        blocks.append(
            f"<div style='margin-top:20px;padding:12px;background:#F8FAFC;border-radius:8px;"
            f"color:#475569;font-size:13px;'>{_e(ctx.file_note)}{link_html}</div>"
        )

    if not ctx.is_success and ctx.error:
        blocks.append(
            f"<h3 style='margin:20px 0 6px;font-size:14px;color:#DC2626;'>Error</h3>"
            f"<pre style='margin:0;padding:12px;background:#FEF2F2;border-radius:8px;"
            f"color:#991B1B;font-size:12px;white-space:pre-wrap;'>{_e(ctx.error)}</pre>"
        )

    body = (
        f"<div style='font-family:Inter,-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        f"max-width:600px;margin:0 auto;padding:24px 16px;color:#0F172A;'>"
        f"<div style='font-size:17px;font-weight:600;margin-bottom:16px;'>"
        f"Automation Run Notification</div>"
        + "".join(blocks)
        + "<div style='margin-top:24px;padding-top:12px;border-top:1px solid #E2E8F0;"
          "color:#94A3B8;font-size:12px;'>This email was generated automatically by the "
          "Zhanlu automation system.</div>"
          "</div>"
    )
    return body


def _render_markdown(text: str) -> str:
    """Render Markdown to email-safe HTML (tables + fenced code).

    Raw HTML in the source is escaped first so LLM-generated summaries can
    never inject markup into the email; the ``markdown`` library then renders
    the safe subset into semantic tags. Inline styles are added to tables and
    code blocks because Gmail and Outlook strip ``<style>`` blocks.
    """
    import markdown  # bundled dependency, also used by artifact previews

    body = markdown.markdown(
        escape(str(text), quote=False),
        extensions=["tables", "fenced_code"],
    )
    body = body.replace(
        "<table>",
        "<table style='border-collapse:collapse;width:100%;margin:10px 0;'>",
    )
    body = body.replace(
        "<th>",
        "<th style='border:1px solid #E2E8F0;padding:6px 8px;background:#F8FAFC;text-align:left;'>",
    )
    body = body.replace(
        "<td>",
        "<td style='border:1px solid #E2E8F0;padding:6px 8px;text-align:left;'>",
    )
    body = body.replace(
        "<pre>",
        "<pre style='background:#F8FAFC;padding:10px;border-radius:6px;overflow-x:auto;'>",
    )
    return body


def _strip_markdown(text: str) -> str:
    """Lightweight Markdown → plain text for the text/plain fallback."""
    out: list[str] = []
    for line in str(text).splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line)             # headings
        line = re.sub(r"^\s*[-*]\s+", "• ", line)          # bullet lists
        line = re.sub(r"^\s*\d+\.\s+", "", line)           # numbered lists
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)   # images
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)  # links → label
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)     # bold
        line = re.sub(r"\*([^*]+)\*", r"\1", line)         # italic
        line = re.sub(r"`([^`]+)`", r"\1", line)           # inline code
        out.append(line)
    return "\n".join(out)


def _table(rows: list[str]) -> str:
    return (
        "<table width='100%' cellpadding='0' cellspacing='0' "
        "style='border-collapse:collapse;border:1px solid #E2E8F0;border-radius:8px;'>"
        + "".join(rows)
        + "</table>"
    )


def _row(label: str, value: str) -> str:
    return (
        "<tr>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #F1F5F9;"
        f"color:#64748B;font-size:13px;width:120px;vertical-align:top;'>{_e(label)}</td>"
        f"<td style='padding:10px 12px;border-bottom:1px solid #F1F5F9;"
        f"color:#0F172A;font-size:13px;'>{value}</td>"
        "</tr>"
    )


def _badge(label: str, color: str, bg: str) -> str:
    return (
        f"<span style='display:inline-block;padding:2px 10px;border-radius:9999px;"
        f"font-size:12px;font-weight:600;color:{color};background:{bg};'>{_e(label)}</span>"
    )


def _b(text: str) -> str:
    return f"<strong>{_e(text)}</strong>"


def _e(text: str) -> str:
    return escape(str(text))


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"
