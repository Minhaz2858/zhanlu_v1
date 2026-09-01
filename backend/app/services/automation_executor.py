"""Automation executor — runs an AutomationTask and produces output files.

Lifecycle of one execution:
  1. Create ``AutomationExecution(status="running", started_at=now)``
  2. Resolve the agent + chat session to use.
  3. Run the agent via the existing chat path (``AgentConversation`` +
     ``add_message``) so we get full tool-calling, KB lookups, and code-exec.
  4. Capture the assistant's final response.
  5. Hand the response to ``document_generator.generate_document`` to produce
     the requested file (HTML, PPTX, DOCX, PDF, JSON, …).
  6. Insert ``AutomationFile`` rows for each generated file.
  7. If the task has ``notify_chat=true``, write a ``ChatMessage`` into the
     user's session with a preview + file links.
  8. Update the execution row (``completed_at``, ``status``, ``output_text``,
     ``error``).

Errors at any step are caught and persisted on the execution row; the
dispatcher can decide whether to retry based on ``attempt < max_retries``.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models.automation_task import AutomationTask
from app.models.automation_execution import AutomationExecution
from app.models.automation_file import AutomationFile
from app.models.chat_message import ChatMessage
from app.models.agent_app import AgentApp
from app.models.agent_conversation import AgentConversation
from app.models.user import User
from app.lib.timezone import format_cst
from app.services.notification_gateway import notify_run_finished

logger = logging.getLogger(__name__)


# Per-run execution id. Set at the top of ``_run_agent_in_conversation`` (the
# executor's agent-run entry point) so the ``execute_automation`` tool handler
# — which runs inside the same thread/event-loop as the agent loop — can read
# the CURRENT execution id and stamp it as the parent of any nested run it
# spawns. This is the only robust signal across the nesting boundary: nested
# runs execute in separate dispatcher tasks/threads where a module-global
# TOOL_CONTEXT would race. Stays None outside an automation run (the
# interactive chat path never sets this), so execute_automation spawned from
# chat gets parent_execution_id=NULL (top-level) — no behaviour change for
# chat.
_CURRENT_EXECUTION_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "automation_current_execution_id", default=None,
)


def get_current_execution_id() -> Optional[str]:
    """Return the execution id of the automation run currently executing in
    this thread/task, or None outside an automation run."""
    return _CURRENT_EXECUTION_ID.get()


def _worker_id() -> str:
    host = os.environ.get("HOSTNAME") or os.environ.get("COMPUTERNAME") or "worker"
    return f"{host}:{os.getpid()}"


def _as_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class _AutomationPaused(Exception):
    """Raised when the agent runtime pauses for confirmation mid-run.

    Pauses are NOT transient in automated mode (no human is present to
    approve), so the executor treats this as terminal — it must not retry
    (retrying would just reproduce the pause and burn the retry budget).
    The message distinguishes the two cases: ``skip_confirmation=true``
    (paused despite the flag — runtime auto-skip not yet supported for this
    pause type) vs ``false`` (paused as configured — user should enable
    skip_confirmation or trigger manually).
    """


class _TaskCreatorMissingError(Exception):
    """The task's created_by_id no longer resolves to a user row.

    Raised before the v3 stream is entered: the stream endpoints enforce
    ownership via ``conv.created_by_id != user.id``, so a missing creator
    otherwise crashes the run with an opaque
    ``'NoneType' object has no attribute 'id'``.
    """


# ---------------------------------------------------------------------------
# Cancellation: ``POST /api/automations/executions/{id}/cancel``
#
# Cancellation is cooperative — the cancel endpoint flips the DB row to
# status="cancelled" (CAS on queued/running) AND sets a threading.Event
# keyed by execution id. The in-flight agent loop polls the event between
# SSE chunks and bails out with ``_AutomationCancelled`` at the next safe
# checkpoint.
#
# Threading.Event (not asyncio.Event) because the event is set from one
# thread (the cancel HTTP handler) and read from another (the executor's
# worker thread which runs its own asyncio loop inside).
# ---------------------------------------------------------------------------

# execution_id -> threading.Event
_CANCEL_EVENTS: Dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def request_cancel(execution_id: str) -> bool:
    """Signal an in-flight agent loop to bail out. Returns True when the
    cancel was delivered to a live event (i.e. an executor is currently
    polling it); False when the run already finished or was never
    registered (the endpoint should still flip the DB row to "cancelled"
    in either case — this just reports whether the in-process signal
    landed).
    """
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(execution_id)
    if ev is None:
        return False
    ev.set()
    return True


def _register_cancel_event(execution_id: str) -> threading.Event:
    """Get-or-create the cancel event for an execution. Idempotent — if
    the executor is retried after a crash, the old event is reset so a
    stale set() can't fire on the new run.
    """
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(execution_id)
        if ev is None:
            ev = threading.Event()
            _CANCEL_EVENTS[execution_id] = ev
        else:
            ev.clear()
        return ev


def _clear_cancel_event(execution_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(execution_id, None)


class _AutomationCancelled(Exception):
    """Raised by the agent loop when the cancel event is set.

    The executor catches this and marks the run ``status="cancelled"``
    with ``error="Cancelled by user"`` and writes a brief "⏹ Run
    cancelled by user" line into the assistant bubble so the user can
    see why the run stopped.
    """


def _resolve_task_user(db, task: AutomationTask) -> User:
    """Resolve the acting user for a run (the task creator).

    The v3 stream / resume endpoints require a REAL user object for their
    ownership check (auth-hardening, 2026-07-28). A ghost creator raises
    ``_TaskCreatorMissingError`` so the executor can fail the run with a
    clear ownership message instead of crashing inside the stream.
    """
    user = None
    user_id = getattr(task, "created_by_id", None)
    if user_id:
        user = db.get(User, user_id)
    if user is None:
        raise _TaskCreatorMissingError(
            f"task creator user {user_id!r} no longer exists"
        )
    return user


_HONESTY_GUARDRAIL = (
    "Report only what tools actually returned. If a data source is "
    "missing, empty, or unreachable, state the issue concisely — never "
    "fabricate rows, totals, or claims."
)

_NO_PREAMBLE = (
    "Produce the deliverable directly — start with your analysis steps "
    "immediately. Do not narrate your boundaries, do not hedge, do not "
    "explain what you can or cannot do, and do not echo guardrail rules. "
    "If the task cannot be completed, state the issue concisely and stop."
)

_FORMAT_GUIDANCE = {
    "html": (
        "Format: the deliverable is an HTML report. Use `##` section "
        "headings and markdown tables of real figures."
    ),
    "docx": (
        "Format: the deliverable is a Word document. Use `##` section "
        "headings, concise paragraphs, and markdown tables of real figures "
        "(tables become native Word tables)."
    ),
    "pptx": (
        "Format: the deliverable is a PowerPoint deck. Structure the answer "
        "as `## <slide title>` sections of short bullet points — each `##` "
        "heading becomes one slide, its bullets the slide body."
    ),
    "pdf": (
        "Format: the deliverable is a PDF document. Use `##` section "
        "headings and markdown tables of real figures."
    ),
    "md": (
        "Format: the deliverable is a Markdown document. Use `##` section "
        "headings and markdown tables of real figures."
    ),
    "xlsx": (
        "Format: the deliverable is an Excel workbook. Emit one JSON block "
        "of rows/records (no prose) so it can be converted to worksheets."
    ),
    "csv": (
        "Format: the deliverable is a CSV file. Emit CSV rows only — a "
        "header row plus data rows, no prose."
    ),
    "json": (
        "Format: the deliverable is a JSON document. Emit a single valid "
        "JSON object, no prose."
    ),
}


_REPORT_FORMATS = {"html", "docx", "pdf", "md"}

_REPORT_STRUCTURE_GUIDANCE = (
    "Structure the deliverable as a professional report:\n"
    "1. `## Executive summary` — at most 3 sentences, outcome first.\n"
    "2. `## Key metrics` — a markdown table: metric | value | delta vs. previous run.\n"
    "3. `## Changes since last run` — concrete bullets (or 'No changes detected').\n"
    "4. `## Anomalies and issues` — or 'None detected'.\n"
    "5. `## Recommended actions` — or 'None required'.\n"
    "Rules: if nothing changed since the previous run, ship a one-page summary, "
    "not a padded report. Never narrate tool calls, connection attempts, or "
    "internal steps in the deliverable body. "
    "This is a business-facing document: NEVER include execution IDs, run "
    "hashes, database hostnames or connection strings, retry policies, raw SQL, "
    "or internal pipeline/config details in the report body or footer. State "
    "results in business terms the reader can act on."
)


def _format_guidance(output_format: str) -> str:
    """Per-format output instruction for the run prompt — aligned with what
    document_generator's parsers expect for each output_format. Prose
    formats also get the mandatory report-structure template."""
    fmt = (output_format or "html").strip().lower()
    if fmt not in _FORMAT_GUIDANCE:
        # Unknown formats fall back to the full HTML report guidance.
        fmt = "html"
    base = _FORMAT_GUIDANCE[fmt]
    if fmt in _REPORT_FORMATS:
        return f"{base}\n\n{_REPORT_STRUCTURE_GUIDANCE}"
    return base


def _filter_skills_by_output_format(
    skill_names: list[str],
    output_format: str,
    db: Optional[Session] = None,
) -> Tuple[list[str], list[str]]:
    """Split ``task.skills`` into ``(compatible, excluded)`` for a given
    output_format.

    Universal skills (no ``compatible_formats`` declared) are always kept.
    Format-bound skills whose declared format matches ``output_format`` are
    kept; the rest are excluded. Fail-safe: on any exception the original
    list is returned unchanged so a transient loader error never drops a
    legitimate skill.
    """
    fmt = (output_format or "html").strip().lower()
    names = list(skill_names or [])
    if not names:
        return [], []

    try:
        from app.services.skills_loader import _resolve_skill_compatible_formats
    except Exception:  # noqa: BLE001
        return names, []

    compatible: list[str] = []
    excluded: list[str] = []
    for name in names:
        try:
            cf = _resolve_skill_compatible_formats(name, db=db)
        except Exception:  # noqa: BLE001
            compatible.append(name)
            continue
        if not cf or fmt in cf:
            compatible.append(name)
        else:
            excluded.append(name)
    return compatible, excluded


def _build_skills_context(task: AutomationTask, db: Session) -> str:
    """Build the progressive-disclosure skills metadata index for a task.

    Returns a compact markdown block (skill name + one-line summary for each
    enabled skill) that gets appended to the agent prompt, or ``""`` when the
    task has no ``skills`` (legacy behavior). The agent is expected to load
    the full ``SKILL.md`` body on demand via the ``skills``/``load_skill_body``
    tool — this index only advertises availability, it does NOT inline the
    bodies, so the context window is preserved.

    Skills whose declared ``compatible_formats`` conflict with
    ``task.output_format`` are silently excluded here (and logged at INFO) so
    a format-bound skill (e.g. pptx) never pollutes the prompt when the user
    requested a different deliverable (e.g. docx). Universal skills
    (research/methodology — no ``compatible_formats``) are always kept.

    This is intentionally non-fatal: any loader failure degrades to ``""``
    (the run continues with the original output_format-only prompt).
    """
    skills = list(task.skills or []) if getattr(task, "skills", None) else []
    if not skills:
        return ""
    try:
        from app.services.skills_loader import get_skill_metadata_for_agent

        output_format = getattr(task, "output_format", None) or "html"
        compatible, excluded = _filter_skills_by_output_format(
            skills, output_format, db=db
        )
        if excluded:
            for name in excluded:
                logger.info(
                    "execute_automation: skipped skill '%s' — incompatible "
                    "with task.output_format '%s' (task_id=%s)",
                    name,
                    output_format.strip().lower(),
                    getattr(task, "id", "?"),
                )
        return get_skill_metadata_for_agent(compatible, db=db) or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "execute_automation: skills metadata build failed (non-fatal): %s",
            exc,
        )
        return ""


def _resolve_task_project(db: Session, task: AutomationTask) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the task's effective ``(project_id, project_name)``.

    Dual-column binding: tasks may carry the FK (``project_id``), the
    legacy name string (``project``), or both. The UI's Resources panel
    matches knowledge bases via EITHER column, so the run must resolve
    the same identity:

    - FK present → ``(task.project_id, <Project.name>)`` — the name feeds
      the legacy-name union in KB resolution (None when the project row
      is missing/deleted).
    - FK NULL + legacy name → deterministically ADOPT the most recently
      updated non-deleted Project with that name (org/app-scoped,
      case-insensitive) so FK-keyed machinery (per-project memory
      conversation, the run conversation's data-source wiring) becomes
      project-scoped. Returns ``(adopted_id_or_None, legacy_name)``.
    - Neither / ``global`` → ``(None, None)`` (workspace-global task).
    """
    from app.models.project import Project
    from app.services.data_source_runtime.data_source_runtime import (
        _normalize_project_name,
    )

    if task.project_id:
        proj = db.get(Project, task.project_id)
        name = _normalize_project_name(getattr(proj, "name", None)) if proj else None
        return task.project_id, name

    legacy = _normalize_project_name(getattr(task, "project", None))
    if not legacy:
        return None, None

    adopted = (
        db.query(Project)
        .filter(
            Project.is_deleted == False,  # noqa: E712
            func.lower(Project.name) == legacy.lower(),
            Project.org_id == getattr(task, "org_id", "default-org"),
            Project.app_id == getattr(task, "app_id", "default-app"),
        )
        .order_by(Project.updated_date.desc())
        .first()
    )
    adopted_id = adopted.id if adopted else None
    if adopted_id:
        # Persist the adopted FK so the binding is FROZEN — subsequent
        # runs resolve via the FK branch (above) instead of re-adopting
        # the latest-updated same-name Project (which would drift when a
        # newer same-name Project appears). Idempotent: only writes when
        # the FK is still NULL.
        if not task.project_id:
            task.project_id = adopted_id
            db.commit()
            logger.info(
                "automation task %s: adopted project_id=%s from legacy project name %r (persisted)",
                task.id, adopted_id, legacy,
            )
        else:
            logger.info(
                "automation task %s: adopted project_id=%s from legacy project name %r",
                task.id, adopted_id, legacy,
            )
    return adopted_id, legacy


def _resolve_bound_data_source_ids(
    db: Session,
    agent: AgentApp,
    project_id,
    project_name: str | None = None,
    pinned_data_source_id: str | None = None,
    include_workspace_auto_bind: bool = True,
) -> list:
    """Effective bound data-source ids for a run — the SAME resolution
    chain the chat tool runtime uses, so the preflight can never disagree
    with what the agent's data tools would see.

    ``project_name`` is the legacy name binding (``knowledge_bases.project``)
    — the UI's Resources panel matches KBs via either column, so the
    preflight must too. ``pinned_data_source_id`` is the task's explicit
    ``data_source_id`` pin; it counts as a bound source when it resolves
    to a non-deleted KB.
    """
    # The underscore helpers live in the inner module — the package's
    # __init__ only re-exports get_bound_data_source_ids.
    from app.services.data_source_runtime import data_source_runtime as _dsrt
    bound = _dsrt.get_bound_data_source_ids(agent)
    if include_workspace_auto_bind:
        bound = _dsrt._maybe_extend_with_workspace_auto_bind(db, agent, bound)
    bound = _dsrt._extend_with_project_kbs(
        db, agent, bound, project_id, project_name=project_name
    )
    if pinned_data_source_id and pinned_data_source_id not in bound:
        from app.models.knowledge_base import KnowledgeBase
        pinned = db.get(KnowledgeBase, pinned_data_source_id)
        if pinned is not None and not pinned.is_deleted:
            bound = list(bound) + [pinned_data_source_id]
    return sorted(bound)


def _pinned_inspection_preflight_error(
    db, task, agent, project_id, project_name: str | None = None,
) -> Optional[str]:
    """Fail-fast preflight for pinned ``agent_inspection`` tasks.

    When a task pins a specific ``data_source_id`` the run MUST NOT
    proceed if that source can't be resolved to a bound KB — otherwise
    the v3 stream's data tools silently see zero sources and the LLM
    answers "no data sources bound" instead of inspecting the database.

    Uses ``_resolve_bound_data_source_ids`` (the same chain the chat tool
    runtime uses) so the preflight can never disagree with what the
    agent's data tools would see. Returns a human, retryable error
    message, or None when the pin resolves / the task isn't pinned
    (data_sync keeps its own preflight). INFO-logs the resolved set every
    run so a future empty-bound case is greppable in production logs.
    """
    task_type = (getattr(task, "type", "") or "").strip().lower()
    if task_type != "agent_inspection":
        return None
    pinned = getattr(task, "data_source_id", None)
    if not pinned:
        return None
    bound = _resolve_bound_data_source_ids(
        db, agent, project_id,
        project_name=project_name,
        pinned_data_source_id=pinned,
    )
    logger.info(
        "execute_automation: agent_inspection preflight task=%s pinned=%s "
        "resolved bound=%s", getattr(task, "id", ""), pinned, bound,
    )
    if pinned not in bound:
        return (
            f"Pinned data source {pinned} is not bound to this task's "
            "project/agent — bind it in My Space → your project → "
            "Resources, then re-run."
        )
    return None


def _probe_db_source(kb, timeout_seconds: float) -> dict | None:
    """SELECT 1 through the canonical connector layer, bounded by a wrapper
    timeout (driver defaults are 10s+). Returns a failure dict or None."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    from app.services.db.connector_factory import DriverUnavailable, get_connector

    def _probe():
        with get_connector(kb) as conn:
            conn.execute("SELECT 1")

    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(_probe)
    try:
        fut.result(timeout=timeout_seconds)
        return None
    except FuturesTimeout:
        return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                "target": f"{kb.host}:{kb.port}",
                "error": f"connection probe timed out after {timeout_seconds}s"}
    except DriverUnavailable as e:
        return {"kb_id": kb.id, "name": kb.name, "kind": "misconfigured",
                "target": f"{kb.host}:{kb.port}", "error": str(e)}
    except Exception as e:
        return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                "target": f"{kb.host}:{kb.port}", "error": str(e)[:200]}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _probe_api_source(kb, timeout_seconds: float) -> dict | None:
    """HEAD (fallback GET) an API-bound source. Failure dict or None."""
    import httpx
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            try:
                resp = client.head(kb.api_url)
                if resp.status_code in (405, 501):
                    resp = client.get(kb.api_url)
            except httpx.RequestError:
                resp = client.get(kb.api_url)
        if resp.status_code >= 500:
            return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                    "target": kb.api_url, "error": f"HTTP {resp.status_code}"}
        return None
    except Exception as e:
        return {"kb_id": kb.id, "name": kb.name, "kind": "unreachable",
                "target": kb.api_url, "error": str(e)[:200]}


def _check_bound_source_connectivity(
    db: Session, bound_ids: list, timeout_seconds: float | None = None,
) -> list[dict]:
    """Probe every bound data source before a data_sync run spends an LLM
    turn. Returns [] when all reachable; otherwise failure dicts
    (kind="unreachable" is retryable, "misconfigured" is not).
    """
    from app.config import settings
    from app.models.knowledge_base import KnowledgeBase

    timeout = timeout_seconds or getattr(
        settings, "AUTOMATION_DS_PREFLIGHT_TIMEOUT_SECONDS", 8.0
    )
    failures: list[dict] = []
    for kb_id in bound_ids:
        kb = db.get(KnowledgeBase, kb_id)
        if kb is None or kb.is_deleted:
            failures.append({
                "kb_id": kb_id, "name": kb_id, "kind": "misconfigured",
                "target": "", "error": "bound data source was deleted",
            })
            continue
        if (kb.source_kind or "").lower() == "file" or (
            not kb.db_type and not kb.api_url
        ):
            continue  # file/manual sources need no connectivity probe
        failure = (
            _probe_db_source(kb, timeout)
            if kb.db_type
            else _probe_api_source(kb, timeout)
        )
        if failure:
            failures.append(failure)
    return failures


class _ApprovalPausedSignal(Exception):
    """Internal control-flow signal: an approval pause was hit and
    ``skip_confirmation`` is on. The outer loop in
    ``_run_agent_in_conversation`` catches it, calls ``_approve_and_resume``
    (approve the pending request + resume the agent), and re-evaluates. This
    is never raised to the executor top level — only ``_AutomationPaused``
    surfaces there (for the non-skippable / cap-exceeded cases)."""


QUOTA_FAILURE_PATTERNS = (
    "429", "quota", "rate limit", "rate_limit", "rate-limit", "payment required", "402",
    "credit", "billing", "insufficient_quota",
)
APPROVAL_FAILURE_PATTERNS = (
    "paused for user confirmation", "paused for a decision summary",
    "auto-approval cap", "auto-approval", "approval pause",
)
NETWORK_FAILURE_PATTERNS = (
    "network", "timed out", "timeout", "connection reset", "connection aborted",
    "upstream timeout", "read timed out",
)

# Driver-level DB drops the API error classifier has no markers for. Checked
# only after the classifier says non-transient, so they can't widen 4xx.
_EXTRA_TRANSIENT_MARKERS = (
    "operationalerror", "database is locked",
    "server closed the connection", "connection lost",
)


def _is_transient_error(error: BaseException) -> bool:
    """Return True if ``error`` is a transient failure worth retrying.

    Status-code-first classification via ``api_error_classifier`` (replaces
    the old substring blocklist that matched "400" anywhere — e.g. "Exported
    400 rows" read as HTTP 400). A small marker list covers driver-level DB
    drops the classifier doesn't know. UNKNOWN is fail-safe (no retry);
    anything non-transient fails fast via _mark_failed_no_retry so we don't
    waste the retry budget on errors that will reproduce.
    """
    from app.services.api_error_classifier import classify_api_error
    from app.services.llm_retry import is_transient

    if is_transient(classify_api_error(error)):
        return True
    text = (str(error) or "").lower()
    return any(p in text for p in _EXTRA_TRANSIENT_MARKERS)


def classify_run_failure_reason(error: str) -> str:
    """Classify an execution error message into a recovery reason.

    The frontend's ``RunFailureActions`` picks a recovery destination based on
    the reason: ``quota`` -> cost settings, ``approval`` -> run history,
    ``network`` -> run history, anything else falls back to a generic
    "view run history" card.  The mapping is intentionally narrow (string
    pattern match, not LLM classification) so it stays stable and reviewable.
    """
    text = (error or "").lower()
    if not text:
        return "unknown"
    for pat in QUOTA_FAILURE_PATTERNS:
        if pat in text:
            return "quota"
    for pat in APPROVAL_FAILURE_PATTERNS:
        if pat in text:
            return "approval"
    for pat in NETWORK_FAILURE_PATTERNS:
        if pat in text:
            return "network"
    return "unknown"


def _mark_failed_no_retry(db: Session, execution: AutomationExecution, error: str) -> None:
    """CAS transition queued/running -> failed WITHOUT scheduling a retry.

    For non-transient failures (e.g. paused-for-confirmation) where retrying
    would just reproduce the same outcome.
    """
    db.execute(
        update(AutomationExecution)
        .where(
            AutomationExecution.id == execution.id,
            AutomationExecution.status.in_(["queued", "running"]),
        )
        .values(
            status="failed",
            error=(error or "")[:5000],
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.refresh(execution)  # pick up the error + output_text we just wrote

    # Phase 4: alert the user's chat (no-retry failures like pauses).
    # Best-effort — never breaks the failure path.
    try:
        t = db.query(AutomationTask).filter(
            AutomationTask.id == execution.automation_task_id,
        ).first()
        if t and str(getattr(t, "notify_chat", "") or "").lower() in ("1", "true", "yes"):
            _notify_chat_failure(db, t, execution, execution.output_text or "")
    except Exception as ne:
        logger.warning("_mark_failed_no_retry: failure notify failed: %s", ne)

    # Phase 4b: email notification gateway (fire-and-forget).
    try:
        if t:
            files = db.query(AutomationFile).filter(
                AutomationFile.execution_id == execution.id
            ).all()
            notify_run_finished(db, t, execution, files, is_success=False)
    except Exception as ne:
        logger.warning("_mark_failed_no_retry: email notify failed: %s", ne)


# ---------------------------------------------------------------------------
# Agent + session resolution
# ---------------------------------------------------------------------------

def _resolve_agent(db: Session, task: AutomationTask) -> Tuple[Optional[AgentApp], str]:
    """Pick the agent to run. Returns ``(agent, reason)``.

    Resolution order:
    1. ``task.agent_id`` if it is valid (exists, not deleted, active).
    2. The hidden ``automation_runtime_agent`` for the task's
       ``(org_id, app_id)`` — created on demand (self-healing).
    3. ``(None, reason)`` only if the runtime agent cannot be created.

    The old behavior ("if 1 workspace agent use it; if many, fail") is
    removed — the runtime agent is always the deterministic fallback.
    """
    if task.agent_id:
        agent = db.query(AgentApp).filter(
            AgentApp.id == task.agent_id,
            AgentApp.is_deleted == False,  # noqa: E712
        ).first()
        if agent and agent.status == "active":
            return agent, "ok"
        logger.warning(
            "automation task %s pins agent_id=%s but it's missing/deleted/inactive; "
            "falling back to runtime agent", task.id, task.agent_id,
        )

    # Deterministic fallback: the hidden runtime agent for this (org, app).
    try:
        from app.services.automation_runtime import ensure_automation_runtime_agent
        runtime = ensure_automation_runtime_agent(db, task.org_id, task.app_id)
        return runtime, "ok"
    except Exception as e:
        logger.exception(
            "_resolve_agent: failed to ensure runtime agent for task %s: %s",
            task.id, e,
        )
        return None, (
            "The backend execution engine is unavailable. "
            "Please contact support."
        )


# Max chars of the previous run's RAW output to keep as a fallback tail
# when structured extraction yields nothing (e.g. the prior output had no
# headings or metrics). Small enough not to blow the context window.
_PREV_CONTEXT_MAX_CHARS = 4000

# Caps for the structured cross-run summary. Headings + metrics + bullets are
# extracted deterministically (no LLM call — keeps scheduled runs cheap and
# latency-free) so the next run gets a compact "what the last run produced"
# instead of a raw head+tail dump. Manus carries a structured "what changed
# since last run" delta; this is the deterministic approximation.
_PREV_SUMMARY_MAX_HEADINGS = 15
_PREV_SUMMARY_MAX_METRICS = 12
_PREV_SUMMARY_MAX_BULLETS = 8
_PREV_SUMMARY_MAX_CHARS = 2500

# Cap on consecutive auto-approvals per run when skip_confirmation=true.
# Bounds runaway agents that keep hitting approval gates so a single
# scheduled run can't loop forever.
MAX_AUTO_APPROVALS = 5


_METRIC_LINE_RE = __import__("re").compile(
    r"[-+]?\d[\d,]*(?:\.\d+)?\s*%|[$€£¥]\s?\d|\d[\d,]*(?:\.\d+)?\s*(?:x|k|m|bn|billion|million|usd|rmb|cny)",
    __import__("re").IGNORECASE,
)


def _extract_structured_summary(text: str) -> dict:
    """Deterministically extract a compact summary of a run's output.

    Pulls markdown headings, metric-looking lines (numbers / percentages /
    currency), and leading bullet points — the signal a human scans for when
    comparing two reports. Returns ``{"title", "headings", "metrics",
    "bullets", "raw_head"}``. All lists are capped. No LLM call: cheap,
    fast, and stable across runs (so the injected context is reproducible).

    Used for cross-run continuity (Tier B #4): the next scheduled run
    receives this summary instead of a raw head+tail truncation, so it can
    produce trend-aware output ("vs. last run: revenue up 12%") the way
    Manus does.
    """
    import re as _re
    if not text:
        return {"title": "", "headings": [], "metrics": [], "bullets": [], "raw_head": ""}

    lines = text.splitlines()
    headings: list[str] = []
    metrics: list[str] = []
    bullets: list[str] = []
    title = ""

    for raw in lines:
        ln = raw.rstrip()
        if not ln.strip():
            continue
        # Headings (## / ### / #).
        hm = _re.match(r"^(#{1,6})\s+(.+)$", ln)
        if hm:
            h = hm.group(2).strip()
            if not title:
                title = h
            if len(headings) < _PREV_SUMMARY_MAX_HEADINGS:
                headings.append(h)
            continue
        # Bullets.
        if ln.lstrip().startswith(("-", "*")) and len(bullets) < _PREV_SUMMARY_MAX_BULLETS:
            bullets.append(ln.lstrip().lstrip("-*").strip())
            continue
        # Metric-looking lines (contain a number with %, currency, or unit).
        if len(metrics) < _PREV_SUMMARY_MAX_METRICS and _METRIC_LINE_RE.search(ln):
            # Keep it short — one line, trimmed.
            metrics.append(ln.strip()[:160])

    raw_head = text.strip()[:400]
    return {
        "title": title[:120],
        "headings": headings,
        "metrics": metrics,
        "bullets": bullets,
        "raw_head": raw_head,
    }


def _previous_run_context(
    db: Session, task: AutomationTask, current_execution_id: str
) -> Tuple[str, Optional[str], Optional[dict]]:
    """Return ``(context_block, prev_execution_id, structured_summary)`` for
    the most recent *successful* run of this task, so the new run can produce
    trend-aware, continuous output (the Manus behavior — weekly reports
    reference prior weeks).

    Tier B #4: instead of injecting the raw previous output (head+tail
    truncation), a structured summary (headings + metrics + bullets) is
    extracted deterministically and injected. The structured summary is also
    returned so the caller can persist it on the execution row for the NEXT
    run's "what changed since last run" delta.

    Returns ``("", None, None)`` if there's no prior completed run.
    """
    try:
        prev = db.query(AutomationExecution).filter(
            AutomationExecution.automation_task_id == task.id,
            AutomationExecution.status == "completed",
            AutomationExecution.id != current_execution_id,
        ).order_by(AutomationExecution.completed_at.desc()).first()
    except Exception as e:
        logger.warning("_previous_run_context: lookup failed: %s", e)
        return "", None, None

    if not prev or not (prev.output_text or "").strip():
        return "", None, None

    text = prev.output_text.strip()
    summary = _extract_structured_summary(text)

    ran_at_str = (
        format_cst(prev.completed_at)
        if prev.completed_at else "unknown"
    )

    # Build a compact, structured context block. Prefer headings + metrics
    # (the scannable signal); fall back to a short raw head if extraction
    # yielded nothing (e.g. free-form prose with no structure).
    parts: list[str] = []
    if summary["headings"]:
        parts.append("**Sections from the previous run:**\n" + "\n".join(
            f"- {h}" for h in summary["headings"]
        ))
    if summary["metrics"]:
        parts.append("**Key figures from the previous run:**\n" + "\n".join(
            f"- {m}" for m in summary["metrics"]
        ))
    if summary["bullets"]:
        parts.append("**Notable points:**\n" + "\n".join(
            f"- {b}" for b in summary["bullets"]
        ))
    if not parts:
        # Nothing structured — fall back to a short raw excerpt.
        excerpt = text[:_PREV_CONTEXT_MAX_CHARS]
        if len(text) > _PREV_CONTEXT_MAX_CHARS:
            excerpt += "\n[…truncated…]"
        parts.append("**Previous output (excerpt):**\n" + excerpt)

    body = "\n\n".join(parts)
    if len(body) > _PREV_SUMMARY_MAX_CHARS:
        body = body[:_PREV_SUMMARY_MAX_CHARS] + "\n\n[…summary truncated…]"

    block = (
        f"## Previous run context\n"
        f"The most recent run of this automation completed at {ran_at_str} "
        f"(execution {prev.id[:8]}). A structured summary of its output is "
        f"below — use it for continuity and trend comparison (e.g. 'vs. last "
        f"run', deltas, changes since the previous report). Do not simply "
        f"repeat it; build on it and explicitly highlight what changed.\n\n"
        f"{body}"
    )
    return block, prev.id, summary


# ---------------------------------------------------------------------------
# LLM-informed tick (2026-08-27): opt-in smart scheduled research.
#
# The dispatcher's "No LLM in the tick" guarantee is preserved by default.
# When AutomationTask.llm_informed_tick is True, EACH fired execution gets
# a lightweight LLM-generated "context preamble" — a fresh interpretation
# of the task prompt in light of NOW + the previous run's summary — so
# scheduled runs behave like Kimi-style smart research instead of a fixed
# script.  The preamble is best-effort: any LLM failure falls back to the
# deterministic prompt unchanged (the run still happens).
# ---------------------------------------------------------------------------

_LLM_TICK_PROMPT_TMPL = (
    "You are the intelligence layer for a scheduled automation task.\n"
    "Task name: {name}\n"
    "Original instructions: {prompt}\n"
    "Current time (UTC): {now}\n"
    "{prev_block}"
    "\nProduce a concise 'run briefing' (max 220 words, plain text, no "
    "markdown headers) that a reporting agent will prepend to its task "
    "prompt. Include: (1) what to focus on THIS run, (2) what changed since "
    "the previous run if any prior context is shown, (3) anything the user "
    "should know (risks, anomalies, follow-ups). Do NOT execute tools; this "
    "is a thinking-only step."
)


def _llm_tick_preamble(
    task: AutomationTask,
    prev_context: str | None,
    db: Session,
) -> str:
    """Best-effort LLM preamble for an llm_informed_tick automation.

    Returns the generated briefing block, or \"\" on any failure (the
    deterministic prompt is used unchanged — the run must never be
    blocked by the enrichment step).
    """
    try:
        if not getattr(task, "llm_informed_tick", False):
            return ""
        prompt = (task.prompt or task.description or task.name or "").strip()
        if not prompt:
            return ""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        prev_block = ""
        if prev_context:
            prev_block = (
                f"Previous run context (provided by the system):\n{prev_context}\n"
            )
        llm_prompt = _LLM_TICK_PROMPT_TMPL.format(
            name=task.name or "unnamed",
            prompt=prompt[:2000],
            now=now_str,
            prev_block=prev_block,
        )
        from app.services.llm_service import call_llm

        # One small call, no tools, tight budget — a tick enrichment must
        # never eat the whole run budget.  temperature=0.3 keeps it stable.
        import asyncio

        resp = asyncio.run(
            call_llm(
                messages=[
                    {"role": "system", "content": (
                        "You are a concise automation briefing writer. "
                        "Return only the briefing text."
                    )},
                    {"role": "user", "content": llm_prompt},
                ],
                temperature=0.3,
            )
        )
        text = (resp or {}).get("response") or (resp or {}).get("content") or ""
        text = str(text).strip()
        if not text:
            return ""
        return (
            f"## Run briefing (LLM-informed tick)\n"
            f"Generated at {now_str} by the scheduled-task intelligence "
            f"layer. Use this as your primary focus for THIS run:\n\n{text}\n"
        )
    except Exception as exc:
        logger.warning(
            "execute_automation: LLM-informed tick preamble failed (non-fatal): %s",
            exc,
        )
        return ""


# ---------------------------------------------------------------------------
# The actual run
# ---------------------------------------------------------------------------

def execute_automation(execution_id: str) -> None:
    """Run a queued automation execution. Top-level entry point used by the
    dispatcher. Loads a fresh DB session and is fully self-contained.
    """
    from app.database import SessionLocal
    from app.config import settings

    db = SessionLocal()
    try:
        execution = db.query(AutomationExecution).filter(
            AutomationExecution.id == execution_id
        ).first()
        if not execution:
            logger.warning("execute_automation: execution %s not found", execution_id)
            return

        # Defense-in-depth recursion cap: if this execution was spawned beyond
        # the allowed nesting depth (e.g. via a path that bypassed the
        # execute_automation tool handler's pre-spawn guard), refuse to run
        # it. The primary guard lives in execute_automation_tool; this catches
        # direct trigger_now/executor invocations too.
        try:
            from app.services.automation_dispatcher import (
                AUTOMATION_MAX_RECURSION_DEPTH, compute_execution_depth,
            )
            if compute_execution_depth(db, execution_id) > AUTOMATION_MAX_RECURSION_DEPTH:
                logger.warning(
                    "execute_automation: %s exceeds recursion cap (depth > %d) — refusing",
                    execution_id, AUTOMATION_MAX_RECURSION_DEPTH,
                )
                _mark_failed(
                    db, execution,
                    f"Recursion cap exceeded (depth > {AUTOMATION_MAX_RECURSION_DEPTH})",
                )
                return
        except Exception as _depth_err:
            logger.warning(
                "execute_automation: depth check failed (non-fatal): %s", _depth_err,
            )

        task = db.query(AutomationTask).filter(
            AutomationTask.id == execution.automation_task_id
        ).first()
        if not task:
            _mark_failed(db, execution, "Parent task not found")
            return

        # CAS transition queued -> running. Guards against duplicate spawns
        # (e.g. a retry firing twice) and stamps the wall-clock deadline the
        # janitor uses to reap hung runs.
        now = datetime.now(timezone.utc)
        rc = db.execute(
            update(AutomationExecution)
            .where(
                AutomationExecution.id == execution_id,
                AutomationExecution.status == "queued",
            )
            .values(
                status="running",
                started_at=now,
                timeout_at=now + timedelta(seconds=settings.AUTOMATION_RUN_TIMEOUT_SECONDS),
                lease_owner=_worker_id(),
            )
        )
        db.commit()
        if rc.rowcount != 1:
            logger.info(
                "execute_automation: %s not in queued state - aborting (likely duplicate)",
                execution_id,
            )
            return
        db.refresh(execution)

        # Auto-prompt injection (per spec): every run immediately drops a
        # "Run Automation Task: ..." user bubble into the task's dedicated
        # chat session, BEFORE the agent starts processing. Without this,
        # the user has no visible signal that the run was triggered — the
        # only "agent is thinking" indicator is the generic activity-steps
        # stream from the v3 stream, which doesn't show WHAT was sent.
        # Idempotent per execution: re-running for the same execution id
        # is a no-op (the marker is matched on phase.execution_id).
        try:
            _post_run_request_marker(db, task, execution, trigger="run")
        except Exception:
            logger.exception(
                "execute_automation: _post_run_request_marker failed (non-fatal) "
                "for execution %s",
                execution_id,
            )

        agent, agent_reason = _resolve_agent(db, task)
        if not agent:
            _mark_failed(db, execution, f"No agent available: {agent_reason}")
            return

        # Resolve the task's project identity ONCE (FK or legacy name)
        # and use it everywhere below — the UI binds data sources via
        # either column, so the run must recognize the same bindings.
        project_id, project_name = _resolve_task_project(db, task)

        # data_sync preflight: with zero bound data sources the run can
        # only fabricate content. Fail fast (retryable — a binding may be
        # added later) instead of spending an LLM run on fiction.
        if (task.type or "").strip().lower() == "data_sync":
            _has_task_scope = bool(
                project_id
                or project_name
                or getattr(task, "data_source_id", None)
            )
            _bound_ids = _resolve_bound_data_source_ids(
                db, agent, project_id,
                project_name=project_name,
                pinned_data_source_id=getattr(task, "data_source_id", None),
                include_workspace_auto_bind=not _has_task_scope,
            )
            if not _bound_ids:
                _mark_failed(
                    db, execution,
                    "No data source bound to this task's project/agent — "
                    "bind one in My Space → your project → Resources, "
                    "then re-run.",
                )
                return
            # Connectivity preflight: a bound-but-unreachable source would
            # otherwise produce a hollow run (the LLM narrates failures as
            # if they were results). Fail fast instead; _mark_failed engages
            # the dispatcher's schedule_retry backoff for transient outages.
            _ds_failures = _check_bound_source_connectivity(db, _bound_ids)
            if _ds_failures:
                _detail = "; ".join(
                    f"{f['name']} ({f['target']}): {f['error']}"
                    for f in _ds_failures[:3]
                )
                if all(f["kind"] == "misconfigured" for f in _ds_failures):
                    _mark_failed_no_retry(
                        db, execution,
                        f"Data source misconfigured — {_detail}",
                    )
                else:
                    _mark_failed(
                        db, execution,
                        f"Data source unreachable — {_detail}",
                    )
                return

        # Pinned agent_inspection preflight (fail-fast): a task that pins
        # a specific data_source_id must NOT silently run with an empty
        # bound set (the LLM would answer "no data sources bound"). The
        # helper resolves the same effective bound set the chat tool
        # runtime would see and returns a retryable error when the pin
        # can't be bound. data_sync has its own preflight above; tasks
        # without a pin keep today's behavior.
        _pin_error = _pinned_inspection_preflight_error(
            db, task, agent, project_id, project_name=project_name,
        )
        if _pin_error:
            _mark_failed(db, execution, _pin_error)
            return

        # Per-project memory: read recent ledger entries for cross-run
        # continuity (the runtime agent builds project knowledge over time).
        _memory_context = ""
        try:
            from app.services.automation_runtime import (
                get_or_create_project_conversation,
            )
            if getattr(agent, "role", None) == "automation_runtime":
                _mem_conv = get_or_create_project_conversation(
                    db, agent, project_id,
                )
                _recent = (_mem_conv.messages or [])[-6:]  # last 6 entries
                if len(_recent) > 1:  # more than just the system init
                    _memory_context = (
                        "\n\n## Recent project memory\n"
                        + "\n".join(
                            m.get("content", "") for m in _recent
                            if m.get("role") != "system"
                        )
                    )
        except Exception as _mem_err:
            logger.debug("execute_automation: memory read failed (non-fatal): %s", _mem_err)

        # Enabled skills (progressive disclosure): inject a compact metadata
        # index (name + one-line summary) so the agent knows which skills are
        # available without blowing up the context window. The agent loads the
        # full SKILL.md body on demand via the `skills`/`load_skill_body` tool.
        # Legacy tasks with no `skills` fall through to the original
        # output_format-only prompt (backward compatible).
        _skills_context = _build_skills_context(task, db)

        # Build a prompt: the task's own prompt + a small note telling the
        # agent what to produce (so the output is structured for the
        # document generator).
        user_prompt = (task.prompt or task.description or task.name or "").strip()
        if not user_prompt:
            _mark_failed(db, execution, "Task has no prompt or description")
            return
        # Cross-run context (Manus parity): pull the most recent successful
        # run's output so this run can reference prior findings/trends
        # instead of starting from a blank slate every time. Tier B #4:
        # inject a STRUCTURED summary (headings + metrics) rather than raw
        # head+tail text, and keep the summary to persist on this run's row
        # for the next run's "what changed" delta.
        prev_context, prev_exec_id, prev_summary = _previous_run_context(db, task, execution.id)
        if prev_context:
            logger.info(
                "execute_automation: %s carrying structured context from prev run %s",
                execution.id, prev_exec_id,
            )
        # LLM-informed tick (opt-in): when the task has llm_informed_tick
        # set, generate a fresh LLM briefing so scheduled runs adapt to
        # NOW + last run instead of replaying a fixed script.  Best-effort:
        # any failure returns "" and the deterministic prompt runs as-is.
        llm_tick_block = _llm_tick_preamble(task, prev_context, db)
        agent_prompt = (
            f"{user_prompt}\n\n"
            f"## Automation context\n"
            f"You are running as part of the scheduled automation '{task.name}' "
            f"({task.id}). Produce a complete, well-structured response — the "
            f"system will save your output as a deliverable for the user.\n\n"
            + _NO_PREAMBLE
            + "\n\n"
            + _format_guidance(getattr(task, "output_format", None))
            + "\n\n"
            + _HONESTY_GUARDRAIL
            + (f"\n\n{llm_tick_block}" if llm_tick_block else "")
            + (f"\n\n{prev_context}" if prev_context else "")
            + (_memory_context or "")
            + (f"\n\n{_skills_context}" if _skills_context else "")
        )

        # Run the agent in a sub-thread with its OWN session and a hard
        # timeout. On timeout we mark the execution failed and return; the
        # orphaned sub-thread is left to finish on its own (its session is
        # isolated, so it can't corrupt ours). This bounds hung LLM calls.
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(_run_agent_in_conversation, task, agent, agent_prompt, execution.id, None, project_id)
        # Persist-state: EVERY exit path (success, timeout, paused, creator
        # missing, generic exception, tool-failure gate) MUST drop a visible
        # assistant bubble into the task's chat session. The chat frontend
        # loads from chat_messages (not agent_conversations.messages JSON), so
        # without this the user sees the auto-prompt user bubble followed by
        # silence on any non-happy-path. The finally block below fires on
        # every return (including the post-success path that may be reaped
        # by the janitor before the assistant text is written back).
        _persist_state: dict = {"user_prompt": None, "assistant_text": None}
        _timeout_s = settings.AUTOMATION_RUN_TIMEOUT_SECONDS
        try:
            try:
                assistant_text, conv_id, fsm_meta, tool_outcome = fut.result(
                    timeout=_timeout_s
                )
            except FuturesTimeout:
                pool.shutdown(wait=False, cancel_futures=True)
                # Refresh so we pick up the partial output the sub-thread
                # persisted via _persist_run_progress before the hang.
                db.refresh(execution)
                _mark_failed(
                    db, execution,
                    f"Run timed out after {_timeout_s}s",
                )
                _persist_state["assistant_text"] = (
                    f"⏱ The run exceeded the {_timeout_s}s time limit "
                    f"and was stopped. The agent may have hit a slow tool "
                    f"call or a network issue. Try again, or simplify the "
                    f"task description to lower the cost."
                )
                return
            except _AutomationPaused as e:
                # Pauses are terminal in unattended mode — don't retry (would
                # just reproduce the pause). Use the no-retry path so the run
                # fails fast with the flag-aware message from the stream.
                pool.shutdown(wait=False, cancel_futures=True)
                logger.warning("execute_automation: %s paused: %s", execution_id, e)
                _mark_failed_no_retry(db, execution, str(e) or "Automation paused for approval")
                _persist_state["assistant_text"] = (
                    f"⏸ The run was paused for approval: "
                    f"{e or 'awaiting operator review'}. "
                    f"Resume it from the task detail page to continue."
                )
                return
            except _AutomationCancelled:
                # User clicked Stop (POST /api/automations/executions/{id}/cancel).
                # The cancel endpoint already CAS-flipped the DB row to
                # "cancelled" before signalling the in-flight thread; we just
                # shut down the pool, drop the running flag, and write a
                # short "⏹ Run cancelled by user" line into the assistant
                # bubble so the user can see why the run stopped.
                pool.shutdown(wait=False, cancel_futures=True)
                logger.info("execute_automation: %s cancelled by user", execution_id)
                _mark_cancelled(db, execution)
                _persist_cancellation_to_chat(db, task, execution)
                _persist_state["assistant_text"] = (
                    "⏹ Run cancelled by user."
                )
                return
            except _TaskCreatorMissingError as e:
                pool.shutdown(wait=False, cancel_futures=True)
                logger.warning("execute_automation: %s %s", execution_id, e)
                _mark_failed_no_retry(
                    db, execution,
                    "Task creator account no longer exists — re-save the "
                    "automation to reassign ownership.",
                )
                _persist_state["assistant_text"] = (
                    "👤 The task creator's account is no longer in the "
                    "system. Re-save the task to assign a new creator, "
                    "then run again."
                )
                return
            except Exception as e:
                pool.shutdown(wait=False, cancel_futures=True)
                logger.exception("execute_automation: agent run failed: %s", e)
                err_msg = f"Agent run failed: {e}"
                _is_transient = _is_transient_error(e)
                if _is_transient:
                    _mark_failed(db, execution, err_msg)  # schedules a retry
                else:
                    _mark_failed_no_retry(db, execution, err_msg)  # fail fast
                _persist_state["assistant_text"] = (
                    f"❌ The agent run failed: {e}\n\n"
                    f"Check the execution logs for full detail. "
                    + (
                        "A retry has been scheduled."
                        if _is_transient
                        else "Please fix the issue and try again."
                    )
                )
                return
            else:
                pool.shutdown(wait=False)
                _persist_state["user_prompt"] = agent_prompt
                _persist_state["assistant_text"] = assistant_text or ""
                # Truthfulness gate (item 5): if EVERY tool call failed and
                # the agent produced nothing real (empty / canned fallback
                # text), the run did not do its work — mark it failed with
                # the actual tool errors (retryable: the data source may
                # recover) instead of shipping boilerplate as a success.
                if _should_fail_for_total_tool_failure(assistant_text, tool_outcome):
                    _errs = "; ".join((tool_outcome.get("errors") or [])[:3]) or "no error detail returned"
                    execution.output_text = (assistant_text or "")[:200_000]
                    logger.warning(
                        "execute_automation: %s failing run — all %d tool call(s) failed: %s",
                        execution_id, tool_outcome["calls"], _errs,
                    )
                    _mark_failed(
                        db, execution,
                        f"All {tool_outcome['calls']} tool call(s) failed — the run could not "
                        f"do its work. First errors: {_errs}",
                    )
                    _persist_state["assistant_text"] = (
                        f"⚠ The run could not complete its work — all "
                        f"{tool_outcome['calls']} tool call(s) failed.\n\n"
                        f"First errors: {_errs}\n\n"
                        f"The data source may be unreachable, or the task "
                        f"description may need adjusting. A retry has been "
                        f"scheduled."
                    )
                    return
                # Append a ledger entry to the per-project memory conversation.
                try:
                    from app.services.automation_runtime import (
                        get_or_create_project_conversation, append_run_summary,
                    )
                    if getattr(agent, "role", None) == "automation_runtime":
                        _mem_conv = get_or_create_project_conversation(
                            db, agent, project_id,
                        )
                        append_run_summary(
                            db, _mem_conv, execution.id, "ok",
                            (assistant_text or "")[:300],
                        )
                except Exception as _mem_err:
                    logger.debug("execute_automation: memory write failed (non-fatal): %s", _mem_err)

            # Abort if the janitor reaped us while we were running.
            db.refresh(execution)
            if execution.status != "running":
                logger.info(
                    "execute_automation: %s was reaped (status=%s) - discarding result",
                    execution_id, execution.status,
                )
                return
        finally:
            # ALWAYS persist the assistant bubble so the chat timeline shows
            # the run's outcome — even on every failure path. The chat
            # frontend polls chat_messages and will replace its optimistic
            # 3-step skeleton with the real assistant bubble as soon as it
            # lands. Idempotent: _persist_run_to_chat skips re-writes for
            # the same execution_id.
            _at = _persist_state.get("assistant_text")
            if _at is not None:
                try:
                    _persist_run_to_chat(
                        db, task, execution,
                        user_prompt=_persist_state.get("user_prompt"),
                        assistant_text=_at,
                    )
                except Exception:
                    logger.exception(
                        "execute_automation: _persist_run_to_chat failed "
                        "(non-fatal) for execution %s",
                        execution_id,
                    )

        # Persist the textual output on the execution row.
        execution.output_text = (assistant_text or "")[:200_000]  # cap at 200KB
        execution.output_data = {
            "agent_id": agent.id,
            "agent_name": agent.name,
            "conversation_id": conv_id,
            # None on the first run / when no prior completed output exists;
            # otherwise the id of the run whose output was carried as context.
            "carried_context_from": prev_exec_id,
            # Truthfulness (item 5): this run's tool-call tally — detail
            # surfaces can show "1 of 17 calls failed" alongside the output.
            "tool_outcome": {
                "calls": tool_outcome.get("calls", 0),
                "failures": tool_outcome.get("failures", 0),
                "errors": (tool_outcome.get("errors") or [])[:5],
            },
            # Tier B #4: persist the structured summary of THIS run's output
            # so the NEXT run can compute a real "what changed since last
            # run" delta. Computed below from the final assistant text.
            # (Filled in after _render_and_save_files so it reflects the
            # shipped output — see the cross_run_delta assignment below.)
            # Tier A #1: record whether the run was forced through the FSM
            # planning pipeline, plus the FSM execution id / state when
            # available, so operators can see the cognitive-core routing.
            "forced_planning": bool(getattr(settings, "AUTOMATION_FORCE_PLANNING", True)),
            "fsm": (
                {
                    "execution_id": fsm_meta.get("execution_id"),
                    "state": fsm_meta.get("state"),
                    "confidence": fsm_meta.get("confidence"),
                }
                if fsm_meta else None
            ),
        }

        # Generate the deliverable file. Tier A #2: pass fsm_meta so the
        # quality gate can hold back low-confidence deliverables (mirrors
        # the chat path's FINALIZE gate).
        try:
            files, quality_gate = _render_and_save_files(
                db, task, execution, assistant_text, agent_prompt,
                fsm_meta=fsm_meta,
            )
        except Exception as e:
            logger.exception("execute_automation: file generation failed: %s", e)
            # Don't fail the whole run — the text output is still useful.
            files, quality_gate = [], None

        # Bug fix (2026-08-13): the assistant bubble was persisted in the
        # inner `finally` above BEFORE any AutomationFile rows existed, so
        # its `artifacts` column was always written as None and the chat
        # never rendered inline preview cards. The file rows are only
        # created here, in `_render_and_save_files`, so re-run
        # `_persist_run_to_chat` to attach the freshly-committed artifacts
        # onto the SAME bubble. The function is idempotent — it finds the
        # existing assistant bubble by execution_id and updates its
        # `artifacts` in place. Guarded on `files` so the failure path
        # (which produces no files) skips the redundant roundtrip.
        if files:
            try:
                _persist_run_to_chat(
                    db, task, execution,
                    user_prompt=_persist_state.get("user_prompt"),
                    assistant_text=_persist_state.get("assistant_text") or assistant_text,
                )
            except Exception:
                logger.exception(
                    "execute_automation: post-render _persist_run_to_chat "
                    "failed (non-fatal) for execution %s",
                    execution_id,
                )

        # Tier B #4: compute the structured summary of THIS run's output and
        # persist it on output_data for the next run's cross-run delta. Done
        # after file generation so it reflects the shipped text.
        try:
            current_summary = _extract_structured_summary(assistant_text or "")
            execution.output_data = {
                **(execution.output_data or {}),
                "cross_run_delta": current_summary,
                # Tier A #2: record the quality-gate outcome so the run
                # history / chat notification can show whether the
                # deliverable was shipped or held back.
                "quality_gate": quality_gate,
            }
        except Exception as _summary_err:
            logger.debug("cross_run_delta persist failed (non-fatal): %s", _summary_err)

        # Final CAS: running -> completed. Only succeeds if the janitor
        # hasn't reaped us in the meantime (prevents overwriting a reaped
        # "failed" status with "completed").
        completed_at = datetime.now(timezone.utc)
        started_at = _as_aware_utc(execution.started_at)
        duration = (
            int((completed_at - started_at).total_seconds())
            if started_at else None
        )
        # Truthfulness: don't report "completed" when the run effectively
        # failed. If every tool call failed (or there were failures and no
        # usable output was produced), mark the run "failed" with a clear
        # error so the UI/scheduler don't present a broken run as a success.
        # Partial failures (some calls failed but output was still produced)
        # remain "completed" with the per-call tally recorded on
        # output_data.tool_outcome so the detail view can surface it.
        tool_calls = (tool_outcome or {}).get("calls", 0)
        tool_failures = (tool_outcome or {}).get("failures", 0)
        final_status = "completed"
        final_error = None
        if tool_calls > 0 and tool_failures >= tool_calls:
            final_status = "failed"
            final_error = (
                f"All {tool_calls} tool call(s) failed during execution"
            )
        elif tool_failures > 0 and not (assistant_text or "").strip():
            final_status = "failed"
            final_error = (
                f"Execution produced no output and {tool_failures} of "
                f"{tool_calls} tool call(s) failed"
            )
        rc = db.execute(
            update(AutomationExecution)
            .where(
                AutomationExecution.id == execution_id,
                AutomationExecution.status == "running",
            )
            .values(
                status=final_status,
                completed_at=completed_at,
                duration_seconds=duration,
                **({"error": final_error} if final_error else {}),
            )
        )
        db.commit()
        if rc.rowcount != 1:
            logger.info(
                "execute_automation: %s could not commit (status changed) - result discarded",
                execution_id,
            )
            return
        db.refresh(execution)

        task.last_run = execution.completed_at.isoformat()
        task.last_run_at = execution.completed_at
        # Append a brief entry to the legacy execution_history list.
        history = list(task.execution_history or [])
        history.append({
            "execution_id": execution.id,
            "status": execution.status,
            "completed_at": execution.completed_at.isoformat(),
            "duration_seconds": execution.duration_seconds,
            "file_count": len(files),
        })
        task.execution_history = history[-50:]  # keep last 50
        db.commit()
        logger.info(
            "execute_automation: %s completed (%d files) for task %s",
            execution.id, len(files), task.id,
        )
        # Phase 4b: email notification gateway. Fire-and-forget — never blocks
        # the run, and a delivery failure never fails the automation.
        try:
            notify_run_finished(db, task, execution, files, is_success=True)
        except Exception as ne:
            logger.warning("execute_automation: email notify failed: %s", ne)
    except Exception as e:
        logger.exception("execute_automation: unhandled error: %s", e)
        try:
            db.rollback()
            execution = db.query(AutomationExecution).filter(
                AutomationExecution.id == execution_id
            ).first()
            if execution:
                _mark_failed(db, execution, f"Unhandled error: {e}")
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def _mark_failed(db: Session, execution: AutomationExecution, error: str) -> None:
    """CAS transition queued/running -> failed, then schedule a retry if
    ``attempt < max_retries``.

    The CAS (``status IN (queued, running)``) prevents the executor and the
    janitor from both marking (and both retrying) the same execution.
    """
    rc = db.execute(
        update(AutomationExecution)
        .where(
            AutomationExecution.id == execution.id,
            AutomationExecution.status.in_(["queued", "running"]),
        )
        .values(
            status="failed",
            error=(error or "")[:5000],
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    if rc.rowcount != 1:
        return  # already terminal - nothing to do

    db.refresh(execution)
    task = db.query(AutomationTask).filter(
        AutomationTask.id == execution.automation_task_id
    ).first()
    if not task:
        return

    from app.services.automation_dispatcher import parse_max_retries, schedule_retry
    max_retries = parse_max_retries(task)
    if execution.attempt < max_retries:
        schedule_retry(task.id, execution.id, execution.attempt, error)
    else:
        logger.info(
            "execution %s failed (attempt %d/%d); no retries left",
            execution.id, execution.attempt, max_retries,
        )
        # Phase 4: alert the user's chat only on the FINAL failure (no retry
        # scheduled) — notifying on an intermediate failure that's about to
        # be retried would produce a confusing "failed then succeeded" pair.
        # Best-effort; never breaks the failure path.
        if str(getattr(task, "notify_chat", "") or "").lower() in ("1", "true", "yes"):
            try:
                _notify_chat_failure(db, task, execution, execution.output_text or "")
            except Exception as ne:
                logger.warning("_mark_failed: failure notify failed: %s", ne)

        # Phase 4b: email notification gateway (fire-and-forget; final failure only).
        try:
            files = db.query(AutomationFile).filter(
                AutomationFile.execution_id == execution.id
            ).all()
            notify_run_finished(db, task, execution, files, is_success=False)
        except Exception as ne:
            logger.warning("_mark_failed: email notify failed: %s", ne)


def _mark_cancelled(db: Session, execution: AutomationExecution) -> bool:
    """CAS transition queued/running -> cancelled. Returns True when the
    row was actually flipped (idempotent — second caller / racing cancel
    + janitor gets ``False`` and bails).

    Unlike :func:`_mark_failed`, a cancelled run NEVER schedules a retry:
    the user explicitly asked to stop, so a re-run would be a surprising
    surprise. The frontend can re-run by clicking Run Now again.
    """
    rc = db.execute(
        update(AutomationExecution)
        .where(
            AutomationExecution.id == execution.id,
            AutomationExecution.status.in_(["queued", "running"]),
        )
        .values(
            status="cancelled",
            error="Cancelled by user",
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return rc.rowcount == 1


def _persist_cancellation_to_chat(
    db: Session,
    task: AutomationTask,
    execution: AutomationExecution,
) -> None:
    """Write a short "⏹ Run cancelled by user" line into the assistant
    bubble that ``_post_run_request_marker`` pre-created for this
    execution. Idempotent: if the marker didn't run (no empty assistant
    bubble exists), appends a fresh one.

    Best-effort: never raises. The cancel endpoint must remain fast.
    """
    try:
        from app.services.automation_sessions import ensure_task_chat_session
        session_id, _ = ensure_task_chat_session(db, task)
        if not session_id:
            return
        # Locate the pre-created empty assistant bubble for this exec.
        existing_asst = None
        for _m in db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id,
        ).all():
            _ph = _m.phase or {}
            if not (isinstance(_ph, dict) and _ph.get("execution_id") == execution.id):
                continue
            if _m.role == "assistant":
                existing_asst = _m
                break
        cancel_line = "⏹ Run cancelled by user."
        if existing_asst is not None:
            existing_asst.content = cancel_line
            existing_asst.phase = {
                **(existing_asst.phase or {}),
                "execution_id": execution.id,
                "automation_task_id": task.id,
                "status": "cancelled",
            }
            db.commit()
            return
        # Fallback: no marker was written, append a new assistant message.
        _max_order = db.query(func.coalesce(func.max(ChatMessage.order), 0)).filter(
            ChatMessage.session_id == session_id,
        ).scalar() or 0
        db.add(ChatMessage(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role="assistant",
            content=cancel_line,
            order=int(_max_order) + 1,
            phase={
                "execution_id": execution.id,
                "automation_task_id": task.id,
                "status": "cancelled",
            },
        ))
        db.commit()
    except Exception as e:
        logger.warning("_persist_cancellation_to_chat failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

def _parse_sse_chunk(chunk) -> Optional[dict]:
    """Parse one SSE chunk ('data: {json}\\n\\n') into a dict, or None."""
    if not isinstance(chunk, str):
        return None
    s = chunk.strip()
    if not s.startswith("data:"):
        return None
    payload = s[len("data:"):].strip()
    if not payload:
        return None
    try:
        import json as _json
        return _json.loads(payload)
    except Exception:
        return None


def _persist_run_progress(
    execution_id: str, steps: list, phase: Optional[str],
    partial_text: Optional[str] = None,
) -> None:
    """Write the current activity_steps + phase to the execution row so the
    Scheduled panel can poll live progress (Manus-style activity feed).

    Also mirrors ``activity_steps`` onto the run's chat message (the user
    bubble carrying the auto-prompt marker, matched by
    ``phase.execution_id``) so the user sees live per-step tool progress
    in their chat timeline. Without this mirror, the chat frontend
    (which polls chat_messages) keeps showing its optimistic
    "Understanding / Analyzing / Preparing" 3-step skeleton because no
    SSE events flow from the automation executor — the executor runs
    in a background thread, not a streaming request handler.

    When ``partial_text`` is supplied, also persist it to ``output_text`` so a
    hung-LLM timeout retains whatever the agent produced before it stalled —
    the executor reads it back on the timeout path and includes it in the
    failure notification. When ``partial_text`` is None, ``output_text`` is
    left untouched (a progress-only write must not clobber existing output).

    Uses its OWN short-lived session — never the agent stream's session — to
    avoid interfering with the stream's transaction management. Safe to call
    from the executor sub-thread. Failures are non-fatal (progress is
    best-effort; the run itself must not depend on it).
    """
    if not execution_id:
        return
    try:
        from app.database import SessionLocal
        pdb = SessionLocal()
        try:
            values: dict = {"activity_steps": list(steps)}
            if phase:
                values["current_phase"] = phase[:50]
            if partial_text is not None:
                values["output_text"] = partial_text[:200_000]
            pdb.execute(
                update(AutomationExecution)
                .where(AutomationExecution.id == execution_id)
                .values(**values)
            )
            pdb.commit()
            # ── Mirror live progress to the chat timeline ──
            # The run's ASSISTANT bubble (pre-created as empty by
            # _post_run_request_marker) is the natural anchor for live
            # activity steps. The chat frontend only renders
            # ActivitySteps on assistant bubbles, so updating the
            # assistant bubble (not the user bubble) is what makes the
            # chat timeline show the real tool-step progress instead
            # of its optimistic 3-placeholders skeleton ("Understanding
            # / Analyzing / Preparing"). The assistant bubble is matched
            # by phase.execution_id; the user bubble carries the same
            # execution_id but we explicitly filter role=assistant.
            # Done in Python (not SQL JSON operator) so it works on both
            # PostgreSQL and SQLite (the test backend).
            try:
                _asst_bubbles = (
                    pdb.query(ChatMessage)
                    .filter(
                        ChatMessage.role == "assistant",
                        ChatMessage.is_deleted == False,  # noqa: E712
                    )
                    .all()
                )
                for _m in _asst_bubbles:
                    _ph = _m.phase or {}
                    if not isinstance(_ph, dict):
                        continue
                    if _ph.get("execution_id") != execution_id:
                        continue
                    # SQLAlchemy JSON column accepts a Python list directly
                    # on both PostgreSQL (native JSON) and SQLite (text
                    # serialization handled by the dialect).
                    _m.activity_steps = list(steps)
                pdb.commit()
            except Exception as _mirror_err:
                logger.debug(
                    "_persist_run_progress: chat mirror failed (non-fatal): %s",
                    _mirror_err,
                )
        finally:
            pdb.close()
    except Exception as e:
        logger.debug("_persist_run_progress: failed (non-fatal): %s", e)


def _approve_and_resume(
    loop: "asyncio.AbstractEventLoop",
    db: Session,
    conv,
    task: AutomationTask,
) -> dict:
    """Approve the pending approval request on ``conv`` and resume the agent
    loop. Used by the ``skip_confirmation`` auto-proceed path.

    ``resume_conversation`` only re-executes the paused tool when the linked
    ApprovalRequest is in the ``approved`` state (otherwise it feeds a denial
    to the LLM). So we approve the request first, then resume. If there's no
    ``approval_id`` (a tool returned ``requires_approval`` without creating a
    request), we still resume — resume feeds a denial and the agent can adapt
    (graceful degradation).

    Failures from ``approve`` (e.g. an already-approved race) are swallowed:
    a double-approve must never abort the run. Returns the dict produced by
    ``resume_conversation``; its ``status`` tells the caller whether the run
    paused again for another approval.
    """
    # Refresh so we see the _resume_state the stream just persisted.
    try:
        db.refresh(conv)
    except Exception:
        pass

    md = getattr(conv, "metadata_", None) or {}
    resume_state = md.get("_resume_state") or {}
    pending_tool = resume_state.get("pending_tool") or {}
    approval_id = pending_tool.get("approval_id")

    if approval_id:
        try:
            from app.services.governance.approval_service import ApprovalService
            ApprovalService(db).approve(
                approval_id, reviewed_by="automation:skip_confirmation",
            )
        except Exception as e:
            logger.warning(
                "execute_automation: auto-approve of %s failed (non-fatal, "
                "resuming anyway): %s", approval_id, e,
            )

    from app.routers.agents import resume_conversation as _resume

    # resume_conversation enforces the same ownership check as the stream —
    # pass the task creator, not a null service user.
    user = _resolve_task_user(db, task)

    async def _do_resume():
        return await _resume(
            app_id=task.app_id or "default-app",
            conversation_id=conv.id,
            db=db,
            user=user,
        )

    return loop.run_until_complete(_do_resume())


def _summarize_tool_outcomes(messages: list) -> dict:
    """Count tool calls/failures in a run conversation.

    The runtime conversation is created fresh per run, so all tool
    activity belongs to THIS run. Two persistence shapes are supported:

    - OpenAI wire format: ``role="tool"`` messages with JSON content
      (keyed by ``tool_call_id``), and
    - the v3 stream's persisted shape: outcomes EMBEDDED in the assistant
      message's ``tool_calls`` entries as ``{"id", "name", ...,
      "results": {...}}``.

    Calls are DEDUPED by id across both shapes and across the repeated
    per-iteration assistant snapshots the stream persists (the same
    tool_calls list appears on every assistant message). Mirrors the
    success-aware semantics of the v3 loop guard (``requires_approval``
    counts as not-failed).
    """
    import json as _json
    seen: set = set()
    calls = 0
    failures = 0
    errors: list = []

    def _record(call_id: str, payload: dict) -> None:
        nonlocal calls, failures
        key = call_id or f"anon-{calls}"
        if key in seen:
            return
        seen.add(key)
        calls += 1
        ok = bool(payload.get("success", True)) or bool(payload.get("requires_approval"))
        if not ok:
            failures += 1
            err = str(payload.get("error") or payload.get("message") or "").strip()
            if err:
                errors.append(err[:200])

    for m in messages or []:
        role = m.get("role")
        if role == "tool":
            try:
                payload = _json.loads(m.get("content") or "{}")
            except (ValueError, TypeError):
                payload = {}
            _record(str(m.get("tool_call_id") or ""), payload)
        elif role == "assistant":
            for tc in (m.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                results = tc.get("results")
                if not isinstance(results, dict):
                    continue
                _record(str(tc.get("id") or ""), results)
    return {"calls": calls, "failures": failures, "errors": errors}


def _is_canned_fallback_output(text: str) -> bool:
    """True when the run's final text is the stream's canned empty-content
    fallback (Agent-Builder boilerplate), i.e. the agent produced NOTHING
    real. Compared against the shared constant so wording changes in one
    place don't silently break the other."""
    from app.routers.agents import _EMPTY_CONTENT_FALLBACK
    return bool(text) and text.strip().startswith(_EMPTY_CONTENT_FALLBACK[:40])


def _should_fail_for_total_tool_failure(output_text: str, outcome: dict) -> bool:
    """Truthfulness gate (item 5): a run whose EVERY tool call failed and
    whose output is empty / the canned fallback did not actually do its
    work — it must be marked failed with the real tool errors, never
    shipped as a success.

    Conservative by design: when all tools failed but the LLM still wrote
    a substantive answer (e.g. honestly explaining the outage), the
    completion stands — that output already reflects what happened.
    """
    if not outcome.get("calls"):
        return False
    if outcome["failures"] != outcome["calls"]:
        return False
    text = (output_text or "").strip()
    return not text or text == "(no response)" or _is_canned_fallback_output(text)


def _heal_conv_agent_name(conv, agent) -> bool:
    """Heal a fallback automation conversation's agent identity.

    ``ensure_task_chat_session`` creates fallback conversations with
    ``agent_name=None`` (see automation_sessions.py). The v3 pre-FSM
    data-source block in agents.py gates the entire
    ``prepare_data_source_runtime`` call on ``if conv.agent_name:`` — a
    falsy value silently skips the block, so ``_v3_data_ctx_extras = {}``
    and the agent runs with ``bound_kb_ids=[]`` → "This agent has no data
    sources bound" even when the task pins a ``data_source_id``.

    Sets the identity ONLY when falsy — origin sessions keep their real
    agent identity (e.g. ``automation_agent``), which also satisfies the
    ``general_assistant`` zero-guard in data_source_runtime.py: assigning
    the task's real agent bypasses that
    guard's "no project → empty" branch for automation runs.

    Returns True when a heal happened.
    """
    if conv is None:
        return False
    if getattr(conv, "agent_name", None):
        return False
    conv.agent_name = agent.name
    return True


def _run_agent_in_conversation(
    task: AutomationTask,
    agent: AgentApp,
    prompt: str,
    execution_id: str = "",
    db_override=None,  # test seam: supply a fake for the creator lookup
    project_id: Optional[str] = None,  # resolved task project (FK or adopted)
) -> Tuple[str, str, Optional[dict], dict]:
    """Create a hidden AgentConversation and run the prompt via the streaming
    agent path, mirroring live activity_steps / phase to the execution row.

    Uses ``add_message_stream`` (the streaming variant of ``add_message``)
    so we can capture the Manus-style numbered activity steps and phase
    headlines as they happen and persist them for the panel to poll. The
    agent's conversation writes use this thread's ``db`` session; the
    progress mirror uses a separate short-lived session per write (see
    ``_persist_run_progress``) so it never interferes with the stream's
    transactions.

    Uses its own DB session (separate from the executor's) — created, used,
    and closed entirely within this (sub-)thread, so it's never shared
    across threads or event loops.

    Returns ``(final_text, conversation_id)``.
    """
    from app.database import SessionLocal

    # Stamp the current execution id into a contextvar so the
    # execute_automation tool handler (running inside this same thread/loop)
    # can record it as the parent of any nested run it spawns. No-op for the
    # interactive chat path, which never enters this function. The token is
    # reset in the finally below so the id can't leak to a reused thread/pool.
    _exec_id_token = (
        _CURRENT_EXECUTION_ID.set(execution_id) if execution_id else None
    )

    # SP2-WS-B: set a per-run LoopState so every tool call in this automation
    # run shares loop-guard history (enables real loop detection). Reset in
    # the finally below so it can't leak to a reused thread/pool.
    from app.services.reliability import LoopState, set_conversation_loop_state
    _loop_state_token = set_conversation_loop_state(LoopState())

    db = SessionLocal()
    conv_id: str = ""
    # Resolve the acting user BEFORE anything else: the v3 stream enforces
    # ownership (conv.created_by_id != user.id), so a ghost creator would
    # crash it with an opaque NoneType AttributeError.
    user = _resolve_task_user(db_override if db_override is not None else db, task)
    # skip_confirmation (Manus "Always skip"): when true the run should
    # auto-proceed past confirmation pauses. The runtime doesn't yet
    # auto-skip, so a pause still surfaces — but we mark it distinctly and
    # never retry it. Threaded into conv.metadata_ so a future runtime
    # auto-skip can read it.
    skip_conf = str(getattr(task, "skip_confirmation", "") or "").lower() in ("1", "true", "yes")
    # Cancel event: ``POST /api/automations/executions/{id}/cancel`` sets it
    # and the agent loop polls it between SSE chunks. Registered here (not
    # inside the try) so the cancel endpoint can find the event even when
    # the run was cancelled BEFORE _consume was reached (e.g. while
    # building the conversation). Cleared in the outer finally below.
    cancel_event = _register_cancel_event(execution_id) if execution_id else None
    try:
        # BUGFIX (one-chat-per-task): reuse the dedicated AgentConversation
        # already created by ``ensure_task_chat_session`` (one per task, all
        # run history accumulates in a single Recent Chat). The previous
        # implementation created a NEW AgentConversation every run, which
        # manifested as N duplicate chat entries per task in Recent Chats.
        # Fall back to a fresh AgentConversation ONLY when
        # ``ensure_task_chat_session`` didn't yield one — should not happen
        # in practice since the helper is also called from
        # ``_post_run_request_marker`` before any run, but the guard keeps
        # us safe against legacy tasks with broken task.session_id links.
        from app.services.automation_sessions import ensure_task_chat_session
        from app.models.chat_session import ChatSession

        try:
            session_id, _created = ensure_task_chat_session(db, task)
        except Exception:
            logger.exception(
                "_run_agent_in_conversation: ensure_task_chat_session failed "
                "for task %s — falling back to fresh conversation", task.id,
            )
            session_id, _created = None, False

        conv: Optional[AgentConversation] = None
        chat: Optional[ChatSession] = None
        if session_id:
            chat = db.query(ChatSession).filter(
                ChatSession.id == session_id,
            ).first()
            if chat is not None and chat.conversation_id:
                conv = db.query(AgentConversation).filter(
                    AgentConversation.id == chat.conversation_id,
                ).first()

        if conv is None:
            # Adoption path: the dedicated chat exists but its
            # ``conversation_id`` was never linked (legacy rows pre-dating
            # the link, or a session created by the frontend without a
            # conversation). Find the most recent AgentConversation already
            # tagged with this task's id and adopt it, so all runs keep
            # accumulating in ONE conversation instead of spawning a fresh
            # one per run. Without this the executor created a new
            # conversation every run — the Recent-Chats duplication the
            # user reported. Ordered newest-first so the latest run's
            # thread becomes the canonical one going forward.
            _recent = (
                db.query(AgentConversation)
                .filter(AgentConversation.org_id == task.org_id)
                .order_by(AgentConversation.created_date.desc())
                .limit(200)
                .all()
            )
            for _c in _recent:
                if (_c.metadata_ or {}).get("automation_task_id") == task.id:
                    conv = _c
                    break

        if conv is None:
            # Fallback path: allocate a fresh AgentConversation tagged with
            # the task's resolved project. Title is the task name (not a
            # per-run title) since this conversation will accumulate all
            # future runs of the same task.
            title = task.name
            conv = AgentConversation(
                id=str(uuid.uuid4()),
                agent_name=agent.name,
                title=title,
                messages=[],
                status="active",
                org_id=task.org_id,
                app_id=task.app_id,
                created_by_id=task.created_by_id,
                # Carry the resolved project so the v3 stream's data-source
                # runtime (agents.py: prepare_data_source_runtime reads
                # conv.project_id) wires the project's bound KBs —
                # ask_data_agent + "Bound Data Sources" prompt section.
                project_id=project_id,
                metadata_={
                    "agent_app_id": agent.id,
                    "automation_task_id": task.id,
                    "data_source_id": getattr(task, "data_source_id", None),
                    "skip_confirmation": skip_conf,
                },
            )
            db.add(conv)
            db.commit()
            db.refresh(conv)
        else:
            # Reused/adopted conversation: keep its existing messages
            # (Manus UX requires the per-task chat to show prior run
            # results) but make sure the metadata carries the current
            # task + agent identity and the project_id matches the
            # resolved one.
            merged_meta = dict(conv.metadata_ or {})
            merged_meta["agent_app_id"] = agent.id
            merged_meta["automation_task_id"] = task.id
            merged_meta["data_source_id"] = getattr(task, "data_source_id", None)
            merged_meta["skip_confirmation"] = skip_conf
            conv.metadata_ = merged_meta
            if conv.project_id != project_id:
                conv.project_id = project_id
            # Heal a fallback conv's agent identity (agent_name=None set
            # by ensure_task_chat_session): without this the v3 pre-FSM
            # data-source block silently skips prepare_data_source_runtime
            # and the agent runs with bound_kb_ids=[] → "This agent has
            # no data sources bound" despite a pinned data_source_id.
            # Only falsy values are healed — origin sessions keep their
            # real agent identity (e.g. automation_agent).
            if _heal_conv_agent_name(conv, agent):
                logger.info(
                    "_run_agent_in_conversation: healed fallback conv %s "
                    "agent_name=%s for task %s", conv.id, agent.name, task.id,
                )
            # Touch updated_date so Recent Chats re-sorts this entry to
            # the top (it had previously been pushed off the page by the
            # per-run duplicates).
            db.commit()

        # Heal the chat ↔ conversation link — ONLY for dedicated automation
        # sessions (agent_name='automation_agent'). For origin sessions
        # (agent_name=None or other), we must NOT overwrite the
        # conversation_id because it belongs to the user's original chat.
        # The agent runtime creates its own conversation for the run, and
        # the ChatMessage rows are written to the origin session via
        # _persist_run_to_chat — the user sees the run output in the same
        # chat without the session's conversation being hijacked.
        if (
            chat is not None
            and chat.conversation_id != conv.id
            and getattr(chat, "agent_name", None) == "automation_agent"
        ):
            chat.conversation_id = conv.id
            db.commit()
        conv_id = conv.id

        # Lazy import to avoid a circular import at module load time.
        from app.routers.agents import add_message_stream
        import asyncio

        loop = asyncio.new_event_loop()
        final_text = ""
        partial_text = ""  # accumulated assistant text for timeout recovery
        activity_steps: list = []
        current_phase: Optional[str] = None
        # FSM metadata captured from the stream's 'done' event (Tier A #2):
        # when the run was routed through SynexiaFSM (force_planning), this
        # carries the confidence score + quality-gate decision so the
        # deliverable can be gated. Stays None for ReAct-loop runs.
        fsm_meta: Optional[dict] = None
        # Tier A #1: force the SynexiaFSM planning pipeline for every
        # scheduled run (Manus always plans before acting). Gated by
        # AUTOMATION_FORCE_PLANNING so it can be disabled without a redeploy.
        try:
            from app.config import settings as _settings
            _force_planning = bool(getattr(_settings, "AUTOMATION_FORCE_PLANNING", True))
        except Exception:
            _force_planning = True
        try:
            asyncio.set_event_loop(loop)

            async def _consume():
                nonlocal final_text, current_phase, partial_text, fsm_meta
                # ``cancel_event`` is the threading.Event registered by the
                # outer scope. We poll it between SSE chunks; if set, we
                # bail out at the next safe checkpoint. Falls back to a
                # no-op event when execution_id is empty (interactive
                # path, never cancelled here).
                nonlocal_cancel_event = cancel_event
                if nonlocal_cancel_event is None:
                    class _NeverSet:
                        def is_set(self): return False
                    nonlocal_cancel_event = _NeverSet()
                # `add_message_stream` is a FastAPI route handler (async def) so
                # calling it returns a coroutine that resolves to a
                # `StreamingResponse`. Iterate the response's body_iterator
                # (the underlying async generator) instead of the coroutine.
                _resp = await add_message_stream(
                    app_id=task.app_id or "default-app",
                    conversation_id=conv.id,
                    body={
                        "role": "user",
                        "content": prompt,
                        # Force FSM planning (Tier A #1). The v3 stream reads
                        # this flag and routes unconditionally through
                        # SynexiaFSM, bypassing the should_trigger_planning
                        # classifier. No effect on normal chat turns.
                        "force_planning": _force_planning,
                        # Tag the user message as an automation phase so the
                        # frontend can render it as an automation card instead of
                        # a standard user bubble.  Execution id is used by the
                        # reactor for run-linking (Tier A #2).
                        "phase": "automation",
                        "automation_task_id": task.id,
                        "automation_execution_id": execution_id,
                    },
                    db=db,
                    user=user,  # task creator — the stream enforces ownership
                )
                async for chunk in _resp.body_iterator:
                    # Cooperative cancel: bail out at the next safe checkpoint
                    # (between SSE chunks). Cheap when not set (one bool read
                    # under a Lock-free Event).
                    if nonlocal_cancel_event.is_set():
                        logger.info(
                            "execute_automation: %s cancelled by user mid-stream",
                            execution_id,
                        )
                        raise _AutomationCancelled()
                    evt = _parse_sse_chunk(chunk)
                    if not evt:
                        continue
                    etype = evt.get("type")
                    if etype == "activity_step":
                        step = evt.get("step") or {}
                        num = step.get("number")
                        replaced = False
                        if num is not None:
                            for i, s in enumerate(activity_steps):
                                if s.get("number") == num:
                                    # running -> done update for the same step
                                    activity_steps[i] = {**s, **step}
                                    replaced = True
                                    break
                        if not replaced:
                            activity_steps.append(dict(step))
                        _persist_run_progress(execution_id, activity_steps, current_phase)
                    elif etype == "phase":
                        current_phase = evt.get("state") or current_phase
                        _persist_run_progress(execution_id, activity_steps, current_phase)
                    elif etype == "plan_summary":
                        # FSM plan decomposition (Tier A #1/#3): render the
                        # plan nodes as pending activity steps so the panel
                        # shows the decomposed plan before/during execution.
                        plan = evt.get("plan") or {}
                        nodes = plan.get("nodes") or []
                        for i, n in enumerate(nodes, start=1):
                            activity_steps.append({
                                "number": i,
                                "description": n.get("name") or "step",
                                "status": "pending",
                                "node_type": n.get("node_type"),
                            })
                        _persist_run_progress(execution_id, activity_steps, current_phase)
                    elif etype == "delta":
                        partial_text += evt.get("content") or ""
                        # Persist periodically to bound DB writes — a hung LLM
                        # timeout then retains whatever was produced so far.
                        if len(partial_text) % 2000 < 50:
                            _persist_run_progress(
                                execution_id, activity_steps, current_phase, partial_text
                            )
                    elif etype == "done":
                        final_text = evt.get("content") or ""
                        partial_text = final_text
                        # Capture FSM metadata (Tier A #2) when the run went
                        # through SynexiaFSM. fsm_confidence is None for
                        # ReAct-loop runs, which the deliverable gate treats
                        # as "no gate" (ship as before).
                        if evt.get("fsm_execution_id") or evt.get("fsm_confidence") is not None:
                            fsm_meta = {
                                "execution_id": evt.get("fsm_execution_id"),
                                "state": evt.get("fsm_state"),
                                "confidence": evt.get("fsm_confidence"),
                                "quality_gate": evt.get("fsm_quality_gate"),
                                "plan_summary": evt.get("fsm_plan_summary"),
                                "artifact_ids": evt.get("fsm_artifact_ids") or [],
                            }
                    elif etype == "error":
                        raise RuntimeError(
                            f"agent stream error: {str(evt.get('message', ''))[:500]}"
                        )
                    elif etype == "paused":
                        reason = evt.get("reason", "")
                        if reason == "awaiting_decision_summary":
                            # Never auto-create agents unattended — even with
                            # skip_confirmation. Fail fast with a clear reason.
                            raise _AutomationPaused(
                                "Agent paused for a decision summary "
                                "(create_agent). This pause type is never "
                                "auto-skipped — trigger the run manually to "
                                "approve agent creation."
                            )
                        # Approval pause. Auto-proceed only when
                        # skip_confirmation is on; otherwise fail as before.
                        if not skip_conf:
                            raise _AutomationPaused(
                                "Agent paused for user confirmation. For "
                                "unattended scheduled runs, enable "
                                "skip_confirmation on the task; otherwise "
                                "trigger the run manually when you can "
                                "approve it."
                            )
                        # skip_conf=True: signal the outer loop to resume.
                        raise _ApprovalPausedSignal()

            # Initial turn.
            try:
                loop.run_until_complete(_consume())
            except _ApprovalPausedSignal:
                pass  # handled by the auto-approve loop below

            # Auto-approve loop: each resume continues the turn and may pause
            # again for another approval. Bounded by MAX_AUTO_APPROVALS so a
            # runaway agent can't loop forever.
            approvals = 0
            while final_text == "" and approvals < MAX_AUTO_APPROVALS:
                approvals += 1
                logger.info(
                    "execute_automation: %s auto-resuming after approval pause "
                    "(#%d/%d)", execution_id, approvals, MAX_AUTO_APPROVALS,
                )
                resumed = _approve_and_resume(loop, db, conv, task)
                still_paused = (
                    isinstance(resumed, dict)
                    and resumed.get("status") == "awaiting_approval"
                )
                if still_paused:
                    continue  # paused again — approve + resume once more
                break  # run reached a terminal state

            if final_text == "" and approvals >= MAX_AUTO_APPROVALS:
                raise _AutomationPaused(
                    f"Run hit the auto-approval cap ({MAX_AUTO_APPROVALS}) — "
                    f"too many consecutive approval pauses. Trigger the run "
                    f"manually to investigate."
                )

            # Detect a decision-summary pause (create_agent interception) hit
            # DURING a resume: resume_conversation returns status="active" for
            # these (only approval pauses set "awaiting_approval"), so status
            # alone can't tell completion from a decision-summary pause. Scan
            # the last assistant message's tool_calls for the marker. Decision-
            # summary pauses are never auto-skipped — fail fast.
            if approvals > 0:
                try:
                    db.refresh(conv)
                except Exception:
                    pass
                for m in reversed(conv.messages or []):
                    if m.get("role") != "assistant":
                        continue
                    for tc in (m.get("tool_calls") or []):
                        if isinstance(tc, dict) and tc.get("status") == "awaiting_decision_summary":
                            raise _AutomationPaused(
                                "Agent paused for a decision summary "
                                "(create_agent) during an auto-resumed run. "
                                "This pause type is never auto-skipped — "
                                "trigger the run manually to approve agent "
                                "creation."
                            )
                    break  # only the last assistant message
        except _AutomationPaused:
            raise
        except _AutomationCancelled:
            # Let the outer executor translate this into a status flip;
            # we still need to return SOMETHING (final_text) so the
            # downstream file-generation / chat-persist steps can run
            # with a sensible (empty) value.
            final_text = ""
            current_phase = "cancelled"
        except Exception as e:
            logger.warning("add_message_stream failed: %s\n%s", e, traceback.format_exc())
            raise
        finally:
            # Always close the loop so its resources are released; otherwise
            # we leak a loop per executed run.
            try:
                loop.close()
            except Exception:
                pass

        # Fallback: read the last assistant message from the persisted
        # conversation if the stream didn't surface a 'done' content. After
        # an auto-resume, the final assistant message is persisted by
        # resume_conversation, so this picks it up.
        if not final_text:
            db.refresh(conv)
            msgs = conv.messages or []
            for m in reversed(msgs):
                if m.get("role") == "assistant" and m.get("content"):
                    final_text = m["content"]
                    break
        # If we accumulated partial streamed text but never got a 'done'
        # (e.g. stream ended without a terminal event), use it as the result.
        if not final_text and partial_text:
            final_text = partial_text
        # Final progress write so the row's output_text is current.
        _persist_run_progress(
            execution_id, activity_steps, current_phase, final_text or partial_text
        )
        # Tool-outcome summary for the executor's truthfulness gate: the
        # conversation is fresh per run, so all tool messages are this
        # run's. Refresh first — the stream wrote them via this session.
        try:
            db.refresh(conv)
        except Exception:
            pass
        tool_outcome = _summarize_tool_outcomes(conv.messages or [])
        return final_text or "(no response)", conv_id, fsm_meta, tool_outcome
    finally:
        if _exec_id_token is not None:
            try:
                _CURRENT_EXECUTION_ID.reset(_exec_id_token)
            except Exception:
                pass
        try:
            from app.services.reliability import reset_conversation_loop_state
            reset_conversation_loop_state(_loop_state_token)
        except Exception:
            pass
        # Always release the cancel event registry slot, even on error.
        # The slot key (execution_id) is per-run, so leaking it would
        # accumulate over time and let stale set() calls fire on future
        # runs that happen to reuse the same uuid.
        if execution_id:
            _clear_cancel_event(execution_id)
        try:
            db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------

def _extract_structured_payload(text: str, output_format: str):
    """If the LLM emitted a JSON code block, prefer it as the structured
    payload (lets the document generator produce nice slides/sections).
    """
    import json
    import re
    if not text:
        return text
    # Look for ```json ... ``` blocks. Greedy on the braces so nested JSON
    # objects/arrays parse correctly (the old non-greedy `\{.*?\}` broke on
    # nested payloads).
    m = re.search(r"```json\s*(\{.*\}|\[.*\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Also try the whole text as JSON if it looks like one.
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except Exception:
            pass
    return text


def _render_and_save_files(
    db: Session,
    task: AutomationTask,
    execution: AutomationExecution,
    assistant_text: str,
    prompt: str,
    *,
    fsm_meta: Optional[dict] = None,
) -> Tuple[list, Optional[dict]]:
    """Render the deliverable in the requested format and persist file rows.

    Tier A #2 (Manus parity): when the run went through SynexiaFSM
    (``fsm_meta`` carries a confidence score), apply the artifact quality
    gate BEFORE generating the file. If confidence is below the shipping
    threshold, the deliverable is HELD BACK — no file is generated or
    persisted — mirroring the chat path's FINALIZE gate so a low-confidence
    scheduled run can't silently ship a broken report. The text output is
    still preserved on the execution row; the chat notification tells the
    user the file was held and why.

    Returns ``(files, quality_gate)`` where ``quality_gate`` is the gate
    decision dict (or None when no gate was applied).
    """
    from app.services.document_generator import generate_document

    output_format = (task.output_format or "html").lower().strip()
    title = task.name or "Automation Report"

    # ── Quality gate (Tier A #2). Only applies when the FSM ran and
    # produced a confidence score; ReAct-loop runs (fsm_meta is None) ship
    # as before. A synthetic artifact id is used so quality_gate_decision
    # fires (it short-circuits on an empty artifact list).
    quality_gate: Optional[dict] = None
    confidence = (fsm_meta or {}).get("confidence")
    if confidence is not None:
        try:
            from app.config import settings
            from app.services.synexia.confidence_scorer import quality_gate_decision
            quality_gate = quality_gate_decision(
                float(confidence),
                ["automation_deliverable"],
                enabled=getattr(settings, "SYNEXIA_QUALITY_GATE_ENABLED", True),
                threshold=getattr(settings, "SYNEXIA_QUALITY_GATE_THRESHOLD", 0.4),
            )
        except Exception as _gate_err:
            logger.warning("quality_gate_decision failed (non-fatal): %s", _gate_err)
            quality_gate = None

    if quality_gate and not quality_gate.get("passed", True):
        logger.warning(
            "execute_automation: deliverable held back by quality gate "
            "(confidence=%.2f < threshold=%.2f) for execution %s",
            quality_gate.get("confidence", confidence),
            quality_gate.get("threshold", 0.4),
            execution.id,
        )
        return [], quality_gate

    payload = _extract_structured_payload(assistant_text, output_format)
    file_id = str(uuid.uuid4())
    file_path, _public_url, mime_type = generate_document(
        output_format=output_format,
        content=payload,
        title=title,
        task_id=task.id,
        exec_id=execution.id,
    )

    size = file_path.stat().st_size if file_path.exists() else 0
    file_row = AutomationFile(
        id=file_id,
        execution_id=execution.id,
        automation_task_id=task.id,
        name=f"{title}.{output_format}",
        file_type=output_format,
        size=size,
        # Authenticated route only — generated files are not served from
        # the public /api/uploads static mount.
        file_url=f"/api/automations/files/{file_id}/download",
        file_path=str(file_path),
        mime_type=mime_type,
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
    )
    db.add(file_row)
    db.commit()
    db.refresh(file_row)
    return [file_row], quality_gate


# ---------------------------------------------------------------------------
# Chat notification
# ---------------------------------------------------------------------------

def _tool_warnings_line(tool_outcome: Optional[dict]) -> str:
    """One-line '⚠ N of M tool calls failed: …' block for the completion
    notification when a run PARTIALLY failed. The run still ships its
    deliverable (some tools succeeded), but the chat output must reflect
    what actually happened instead of reading as a clean success.
    """
    if not tool_outcome:
        return ""
    failures = tool_outcome.get("failures") or 0
    calls = tool_outcome.get("calls") or 0
    if not failures or not calls:
        return ""
    errs = "; ".join((tool_outcome.get("errors") or [])[:2])
    line = f"**⚠️ {failures} of {calls} tool calls failed this run**"
    if errs:
        line += f": {errs}"
    return line + "\n\n"


_SENTENCE_ENDINGS = (". ", "! ", "? ", "。", "！", "？", "\n")


def _summarize_preview(text: str, cap: int = 600, max_sentences: int = 3) -> str:
    """First-N-sentences summary that never cuts mid-sentence.

    Falls back to a word-boundary cut only when a single sentence exceeds
    ``cap`` on its own.
    """
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    sentences: list[str] = []
    consumed = 0
    rest = text
    while rest and len(sentences) < max_sentences:
        cut = -1
        for end in _SENTENCE_ENDINGS:
            idx = rest.find(end)
            if idx != -1 and (cut == -1 or idx + len(end.rstrip()) < cut):
                cut = idx + len(end.rstrip())
        if cut <= 0:
            break
        sentence = rest[:cut].strip()
        if not sentence or consumed + len(sentence) > cap:
            break
        sentences.append(sentence)
        consumed += len(sentence)
        rest = rest[cut:].lstrip()
    if sentences:
        return " ".join(sentences) + " …"
    return text[:cap].rsplit(" ", 1)[0] + "…"


_FORMAT_LABELS = {
    "docx": "Word document",
    "html": "Web page",
    "pdf": "PDF",
    "md": "Markdown",
    "pptx": "PowerPoint",
    "xlsx": "Spreadsheet",
    "csv": "CSV",
    "json": "JSON",
}


def _format_label(fmt: str | None) -> tuple[str, str]:
    """Return ``(human_label, raw_code)`` for an output format code.

    The raw code is echoed next to the label so the user sees both the
    friendly name and the underlying file type (e.g. ``Word document (docx)``).
    Unknown codes fall back to the raw value itself.
    """
    raw = (fmt or "html").strip() if fmt else "html"
    return _FORMAT_LABELS.get(raw.lower(), raw), raw


def _bump_session_unread(db: Session, session_id: str) -> None:
    """Bump a chat session's metadata so it surfaces at the top of the
    sidebar's recent-conversations list after an automation run.

    * ``updated_date`` — the sidebar sorts by ``-updated_date``
      (``ChatSession.list('-updated_date', 100)``), so refreshing it moves
      the session to the top.
    * ``last_message_at`` — ISO display string read by the frontend.
    * ``unread`` — the sidebar renders a blue dot; cleared when the user
      opens the session (frontend mark-read on ``selectSession``).

    Never raises — a metadata bump failure must not block the run.
    """
    try:
        from app.models.chat_session import ChatSession
        sess = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if sess is None:
            return
        _now = datetime.now(timezone.utc)
        sess.last_message_at = _now.isoformat()
        sess.updated_date = _now
        sess.unread = True
        db.commit()
    except Exception as _bump_err:
        logger.warning("_bump_session_unread: failed (non-fatal): %s", _bump_err)
        try:
            db.rollback()
        except Exception:
            pass


def _persist_run_to_chat(
    db: Session,
    task: AutomationTask,
    execution: AutomationExecution,
    *,
    user_prompt: str,
    assistant_text: str,
) -> None:
    """Save the run's assistant reply into the task's origin chat session
    (ChatMessage rows).

    The chat frontend loads its timeline from ``chat_messages`` (not from
    ``agent_conversations.messages``), so without this persist step the run
    output lives only in the conversation JSON and the user sees an empty
    timeline after "Run now". We write both sides:

    * a user bubble carrying the auto-prompt marker (the "Run Automation
      Task: ..." record posted by ``_post_run_request_marker`` at the start
      of the run) — already in the DB by the time we get here, so this
      function only writes it if the marker is missing (defensive fallback
      for the pre-marker call path).
    * an assistant bubble carrying the run's final reply — written on
      EVERY run that produced output, even if the marker already exists.

    The assistant bubble is paired with the one pre-created by
    ``_post_run_request_marker`` (``phase.execution_id = execution.id``,
    role=assistant, content=empty). When the run finishes, this function
    UPDATES that bubble's content in place — the chat frontend shows
    live activity_steps on the assistant bubble (mirrored by
    ``_persist_run_progress``) followed by the final reply in the same
    bubble. The 3-placeholders skeleton the user was seeing gets replaced
    with a real card as soon as the run starts.

    Idempotent per execution for the user side (skip if the marker is
    there); the assistant side UPDATES the existing bubble in place
    rather than creating a new one. Never raises — a persist failure
    must not block the run.
    """
    try:
        from app.services.automation_sessions import ensure_task_chat_session
        session_id, _ = ensure_task_chat_session(db, task)
        if not session_id:
            return

        # Discover what's already been written for this execution.
        _existing_user_msg = None
        _existing_asst_msg = None
        for _m in db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id,
        ).all():
            _ph = _m.phase or {}
            if not (isinstance(_ph, dict) and _ph.get("execution_id") == execution.id):
                continue
            if _m.role == "user":
                _existing_user_msg = _m
            elif _m.role == "assistant":
                _existing_asst_msg = _m

        # Pick a monotonically increasing order number based on the
        # existing max order in this session (matches the convention the
        # frontend chat uses — small int sequence, NOT a millisecond
        # timestamp which would overflow Integer).
        _max_order = db.query(func.coalesce(func.max(ChatMessage.order), 0)).filter(
            ChatMessage.session_id == session_id,
        ).scalar() or 0
        _base_order = int(_max_order) + 1

        common_phase = {
            "execution_id": execution.id,
            "automation_task_id": task.id,
        }

        # NOTE: The user bubble is now created by _post_run_request_marker
        # at the start of the run. This function only writes a defensive
        # fallback user bubble if the marker is missing (e.g. the marker
        # call failed or was skipped). The assistant bubble is always
        # updated in place (the pre-created empty one from the marker).

        # Defensive fallback: create user bubble if marker didn't run.
        if not _existing_user_msg and user_prompt:
            _label, _raw = _format_label(task.output_format)
            _user_lines = [
                f"Run Automation Task：",
                f"- Name：{task.name or '(untitled)'}",
                f"- Type：{task.type or 'general'}",
                f"- Output format：{_label} ({_raw})",
                f"- Project：{task.project or '(none)'}",
            ]
            if task.description:
                _user_lines.append(f"- Description：{task.description}")
            _user_content = "\n".join(_user_lines)
            db.add(ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                content=_user_content,
                order=_base_order,
                tool_calls=None,
                activity_steps=None,
                artifacts=None,
                phase={
                    **common_phase,
                    "verb": "▶",
                    "title": "Run Automation Task",
                    "trigger": "run",
                },
                org_id=task.org_id,
                app_id=task.app_id,
                created_by_id=task.created_by_id,
            ))
            _base_order += 1

        if assistant_text:
            # Attach the run's file outputs so the chat renders inline
            # preview cards (partitionArtifacts keys off
            # source == "automation_file"). Soft-deleted files are skipped.
            _files = db.query(AutomationFile).filter(
                AutomationFile.execution_id == execution.id,
                AutomationFile.is_deleted == False,
            ).all()
            _artifacts = _chat_message_artifacts_for_files(_files) if _files else None

            if _existing_asst_msg is not None:
                # Update the empty pre-created assistant bubble in place:
                # this is the slot the 3-placeholders skeleton was
                # occupying. Mirroring the final reply into the same row
                # (not creating a new bubble) keeps the run as a single
                # timeline entry: marker → live steps → final reply.
                _existing_asst_msg.content = assistant_text
                _ph = dict(_existing_asst_msg.phase or {})
                _ph["verb"] = "🤖"
                _ph["title"] = task.name or "Run result"
                _ph["live"] = False  # mark finalized so the frontend
                # can stop showing "running" affordances.
                _existing_asst_msg.phase = _ph
                _existing_asst_msg.artifacts = _artifacts
            else:
                # Defensive fallback: no pre-created assistant bubble
                # (e.g., the marker was written by an older code path
                # that didn't pre-create). Write a fresh one.
                db.add(ChatMessage(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    role="assistant",
                    content=assistant_text,
                    order=_base_order,
                    tool_calls=None,
                    activity_steps=None,
                    artifacts=_artifacts,
                    phase={
                        **common_phase,
                        "verb": "🤖",
                        "title": task.name or "Run result",
                    },
                    org_id=task.org_id,
                    app_id=task.app_id,
                    created_by_id=task.created_by_id,
                ))

        db.commit()
        # Surface the session at the top of the recent list + mark unread.
        _bump_session_unread(db, session_id)
    except Exception as _persist_err:
        logger.warning(
            "_persist_run_to_chat: chat persist failed (non-fatal): %s",
            _persist_err,
        )
        try:
            db.rollback()
        except Exception:
            pass


def _post_run_request_marker(
    db: Session,
    task: AutomationTask,
    execution: AutomationExecution,
    *,
    trigger: str,  # "run" | "retry" | "scheduled"
) -> None:
    """Pre-create a user bubble + an empty assistant bubble for the run.

    The user bubble is a synthetic "Run Automation Task: …" card that
    shows the task's 5-bullet summary (Name, Type, Output format,
    Project, Description) so the user sees WHAT was triggered before
    the agent starts responding.

    The empty assistant bubble is needed so that:
      * ``_persist_run_progress`` mirrors live activity_steps onto it
        (so the chat frontend — which only renders ActivitySteps on
        assistant bubbles — shows the real tool-step progress instead
        of its 3-placeholder optimistic skeleton)
      * ``_persist_run_to_chat`` sets the final content (success reply
        or failure message) on it

    Both bubbles are tagged with ``phase.execution_id = execution.id``
    so retries / reaper-cleaned replays can find them again.
    Idempotent per execution; never raises — a marker failure must
    not block the run.
    """
    try:
        from app.services.automation_sessions import ensure_task_chat_session
        session_id, _ = ensure_task_chat_session(db, task)
        if not session_id:
            return

        # Idempotent: skip user bubble if already exists for this execution.
        _have_user = False
        _have_asst = False
        for _m in db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id,
        ).all():
            _ph = _m.phase or {}
            if not isinstance(_ph, dict):
                continue
            if _ph.get("execution_id") != execution.id:
                continue
            if _m.role == "user":
                _have_user = True
            elif _m.role == "assistant":
                _have_asst = True

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # User bubble: synthetic "Run Automation Task: …" card showing
        # the task's 5-bullet summary so the user sees WHAT was triggered
        # before the agent starts responding.
        if not _have_user:
            _label, _raw = _format_label(task.output_format)
            _user_lines = [
                f"Run Automation Task：",
                f"- Name：{task.name or '(untitled)'}",
                f"- Type：{task.type or 'general'}",
                f"- Output format：{_label} ({_raw})",
                f"- Project：{task.project or '(none)'}",
            ]
            if task.description:
                _user_lines.append(f"- Description：{task.description}")
            _user_content = "\n".join(_user_lines)
            user_msg = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                content=_user_content,
                order=now_ts,
                tool_calls=None,
                activity_steps=None,
                artifacts=None,
                phase={
                    "verb": "▶",
                    "title": "Run Automation Task",
                    "execution_id": execution.id,
                    "automation_task_id": task.id,
                    "trigger": trigger,
                },
                org_id=task.org_id,
                app_id=task.app_id,
                created_by_id=task.created_by_id,
            )
            db.add(user_msg)

        if not _have_asst:
            # Empty assistant bubble — becomes the run's live-progress
            # card and final response carrier. order = user_bubble_order + 1
            # so it sorts directly below the user bubble. The chat frontend
            # only renders ActivitySteps on assistant bubbles, so this is
            # the slot the 3-placeholders skeleton was occupying.
            asst_msg = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                content="",
                order=now_ts + 1,
                tool_calls=None,
                activity_steps=[],
                artifacts=None,
                phase={
                    "verb": "🤖",
                    "title": task.name or "Automation Run",
                    "execution_id": execution.id,
                    "automation_task_id": task.id,
                    "trigger": trigger,
                    "live": True,
                },
                org_id=task.org_id,
                app_id=task.app_id,
                created_by_id=task.created_by_id,
            )
            db.add(asst_msg)

        db.commit()
        # Surface the session at the top of the recent list + mark unread
        # as soon as the run starts (live progress becomes visible).
        _bump_session_unread(db, session_id)
    except Exception:
        logger.exception(
            "post_run_request_marker: failed (non-fatal) for execution %s",
            execution.id,
        )


def _notify_chat(
    db: Session,
    task: AutomationTask,
    execution: AutomationExecution,
    assistant_text: str,
    files: list[AutomationFile],
    tool_outcome: Optional[dict] = None,
) -> None:
    """Drop a ChatMessage into the user's chat with a preview + file links.

    The user sees this in their main chat list as a "scheduled update" from
    the agent — the Manus-style behavior.
    """
    if not task.session_id:
        # No origin session — find or create a default session for the user.
        from app.models.chat_session import ChatSession
        sess = db.query(ChatSession).filter(
            ChatSession.created_by_id == task.created_by_id,
            ChatSession.is_deleted == False,
        ).order_by(ChatSession.created_date.desc()).first()
        if not sess:
            return  # nothing to attach to
        session_id = sess.id
    else:
        session_id = task.session_id

    # Build the preview: first few whole sentences (never mid-sentence).
    preview = _summarize_preview(assistant_text)

    file_links = "\n".join(
        f"- [{f.name}]({f.file_url})" for f in files
    ) if files else ""

    body = (
        f"**📅 Scheduled update: {task.name}**\n\n"
        f"{preview}\n\n"
        + _tool_warnings_line(tool_outcome)
        + (f"**Generated files:**\n{file_links}\n\n" if file_links else "")
        + f"_Ran at {format_cst(datetime.now(timezone.utc))} · "
        f"execution id `{execution.id[:8]}`_"
    )

    msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=body,
        order=int(datetime.now(timezone.utc).timestamp()),
        tool_calls=None,
        activity_steps=None,
        artifacts=(
            _chat_message_artifacts_for_files(files) if files else None
        ),
        phase={
            "verb": "📅",
            "title": f"Scheduled update: {task.name}",
            # Enabling fields for Manus-style per-run navigation: the
            # frontend can turn this chat message into a deep link to the
            # exact run in the Scheduled panel (open execution details,
            # scroll to the report).
            "execution_id": execution.id,
            "automation_task_id": task.id,
        },
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
    )
    db.add(msg)
    db.commit()
    execution.notified_session_id = session_id
    db.commit()
    # Success notification → surface the session + mark unread.
    _bump_session_unread(db, session_id)


def _chat_message_artifacts_for_files(files: list[AutomationFile]) -> list[dict]:
    """Local proxy for ``automation_api._chat_message_artifacts_for_files``.

    The success notification writes a ``ChatMessage`` JSON row directly
    (bypassing the router), so we mirror the contract here to keep the chat
    artifact shape in lockstep with the run and detail surfaces.
    """
    from app.routers.automation_api import _chat_message_artifacts_for_files as _impl
    return _impl(files)


def _notify_chat_failure(
    db: Session,
    task: AutomationTask,
    execution: AutomationExecution,
    partial_output: str,
) -> Optional["ChatMessage"]:
    """Drop a ChatMessage into the user's chat alerting them that a scheduled
    run failed, including the error and any partial output the agent produced
    before the failure (so the user isn't left with a silent gap).

    Mirrors ``_notify_chat`` (the success path) but is called from the failure
    paths (``_mark_failed`` / ``_mark_failed_no_retry``). Returns the created
    ChatMessage, or ``None`` if there's no session to notify.
    """
    if not task.session_id:
        from app.models.chat_session import ChatSession
        sess = db.query(ChatSession).filter(
            ChatSession.created_by_id == task.created_by_id,
            ChatSession.is_deleted == False,  # noqa: E712
        ).order_by(ChatSession.created_date.desc()).first()
        if not sess:
            return None  # nothing to attach to
        session_id = sess.id
    else:
        session_id = task.session_id

    preview = (partial_output or "").strip()
    if len(preview) > 600:
        preview = preview[:600].rsplit(" ", 1)[0] + "…"

    err = (execution.error or "Run failed")[:500]
    body = (
        f"**⚠️ Scheduled run failed: {task.name}**\n\n"
        f"**Error:** {err}\n\n"
        + (f"**Partial output:**\n{preview}\n\n" if preview else "")
        + f"_Failed at {format_cst(datetime.now(timezone.utc))} · "
        f"execution id `{execution.id[:8]}`_"
    )

    msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=body,
        order=int(datetime.now(timezone.utc).timestamp()),
        tool_calls=None,
        activity_steps=None,
        artifacts=None,
        phase={
            "verb": "⚠️",
            "title": f"Scheduled run failed: {task.name}",
            "execution_id": execution.id,
            "automation_task_id": task.id,
            "failed": True,
        },
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
    )
    db.add(msg)
    db.commit()
    execution.notified_session_id = session_id
    db.commit()
    # Failure notification → surface the session + mark unread too, so a
    # failed automation is never a silent event.
    _bump_session_unread(db, session_id)
    return msg


__all__ = ["execute_automation"]
