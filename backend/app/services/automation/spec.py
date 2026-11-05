"""AutomationSpec validation — canonical server-side validation module (R2).

Normative schema: docs/superpowers/specs/2026-11-03-zhanlu-automation-technical-specification.md §2.

Pure, deterministic, LLM-free: cron parsing uses
``schedule_parser.parse_schedule_rules_only`` (never the LLM fallback) and
next-run resolvability uses ``schedule_parser.next_run_at``. Existence checks
(project, agent) run only when a ``db`` session is supplied; without one the
structural checks still apply (draft PATCH validation works statelessly).

Return shape (stored on ``AutomationDraft.validation_errors``):
    {"ok": bool, "errors": [{"field", "code", "message"}, ...]}
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.schedule_parser import (
    parse_schedule_rules_only,
    next_run_at,
)

# ---------------------------------------------------------------------------
# Vocabulary + ceilings (org-level overrides arrive with the Control Plane;
# these module constants are the platform defaults per TS §2).
# ---------------------------------------------------------------------------

TRIGGER_TYPES = ("schedule", "once", "manual")
OUTPUT_FORMATS = ("html", "docx", "pptx", "md", "json", "none")
NOTIFY_ON = ("always", "on_success", "on_failure")
MISFIRE_POLICIES = ("fire_once_late", "skip")
APPROVAL_POLICIES = ("block",)

# Tool classes eligible for unattended pre-approval (TS §2 approvals).
# Kept deliberately read-biased; the org taxonomy ratification (open
# decision #2) may extend this list via settings.
DEFAULT_PRE_APPROVED_CLASSES = (
    "db.read",
    "web.read",
    "file.create",
)

# Platform ceilings (org ceilings may only lower these).
MAX_WALL_CLOCK_SECONDS = 3600
MAX_TOOL_CALLS = 200
MAX_LLM_CALLS = 120
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_RETRY_ATTEMPTS = 5
MAX_NAME_LENGTH = 255

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _err(errors: list, field: str, code: str, message: str) -> None:
    errors.append({"field": field, "code": code, "message": message})


def _validate_trigger(trigger: dict, errors: list) -> None:
    ttype = trigger.get("type", "schedule")
    if ttype not in TRIGGER_TYPES:
        _err(errors, "trigger.type", "invalid", f"unknown trigger type {ttype!r}")
        return

    if ttype == "schedule":
        cron_raw = trigger.get("cron")
        if not cron_raw or not str(cron_raw).strip():
            _err(errors, "trigger.cron", "required", "schedule triggers require a cron expression")
        else:
            cron = parse_schedule_rules_only(str(cron_raw))
            if not cron:
                _err(errors, "trigger.cron", "unparseable", f"could not parse schedule {cron_raw!r}")
            else:
                # Next-run resolvability: a parseable cron that never fires
                # (e.g. Feb 30) must not pass silently.
                tz_name = trigger.get("timezone") or "UTC"
                try:
                    next_run_at(cron, after=datetime.now(timezone.utc), tz_name=tz_name)
                except Exception:  # noqa: BLE001 — any resolution failure is loud
                    _err(errors, "trigger.cron", "unresolvable", "cron never fires in the given timezone")

        _validate_timezone(trigger, errors)
        policy = trigger.get("misfire_policy", "fire_once_late")
        if policy not in MISFIRE_POLICIES:
            _err(errors, "trigger.misfire_policy", "invalid", f"unknown misfire policy {policy!r}")
        grace = trigger.get("grace_window_seconds", 3600)
        if not isinstance(grace, int) or grace < 0 or grace > 7 * 24 * 3600:
            _err(errors, "trigger.grace_window_seconds", "invalid", "grace window must be 0..7 days")

    elif ttype == "once":
        _validate_timezone(trigger, errors)
        run_at = trigger.get("run_at")
        if not run_at:
            _err(errors, "trigger.run_at", "required", "once triggers require run_at")
        else:
            try:
                when = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when <= datetime.now(timezone.utc):
                    _err(errors, "trigger.run_at", "past", "run_at must be in the future")
            except ValueError:
                _err(errors, "trigger.run_at", "invalid", "run_at must be an ISO 8601 datetime")

    # manual: no schedule fields required.
    if ttype in ("schedule", "once") and not trigger.get("timezone"):
        # timezone is REQUIRED for schedule/once (TS §2 comment) — defaulted
        # specs carry it explicitly; absence means the proposer dropped it.
        _err(errors, "trigger.timezone", "required", "schedule/once triggers require an IANA timezone")


def _validate_timezone(trigger: dict, errors: list) -> None:
    tz_name = trigger.get("timezone")
    if not tz_name:
        return  # required-check handled by the caller where applicable
    try:
        ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        _err(errors, "trigger.timezone", "invalid_timezone", f"unknown IANA timezone {tz_name!r}")


def _validate_output(output: dict, errors: list) -> None:
    fmt = output.get("format", "html")
    if fmt not in OUTPUT_FORMATS:
        _err(errors, "output.format", "invalid", f"output.format must be one of {OUTPUT_FORMATS}")
    cap = output.get("artifact_byte_cap", 10485760)
    if not isinstance(cap, int) or cap < 0 or cap > MAX_ARTIFACT_BYTES:
        _err(errors, "output.artifact_byte_cap", "over_ceiling", f"artifact_byte_cap must be 0..{MAX_ARTIFACT_BYTES}")


def _validate_delivery(delivery: dict, errors: list) -> None:
    for channel in ("chat", "email"):
        val = delivery.get(channel, False)
        if not isinstance(val, bool):
            _err(errors, f"delivery.{channel}", "invalid", "must be a boolean")


def _validate_notifications(notifications: dict, errors: list) -> None:
    notify_on = notifications.get("notify_on", "on_failure")
    if notify_on not in NOTIFY_ON:
        _err(errors, "notifications.notify_on", "invalid", f"notify_on must be one of {NOTIFY_ON}")
    emails = notifications.get("emails", [])
    if not isinstance(emails, list):
        _err(errors, "notifications.emails", "invalid", "emails must be a list")
        return
    for e in emails:
        if not isinstance(e, str) or not _EMAIL_RE.match(e):
            _err(errors, "notifications.emails", "invalid", f"invalid email address {e!r}")
            break


def _validate_approvals(approvals: dict, errors: list) -> None:
    policy = approvals.get("policy", "block")
    if policy not in APPROVAL_POLICIES:
        _err(errors, "approvals.policy", "invalid", f"unknown approval policy {policy!r}")
    scope = approvals.get("pre_approved_scope", [])
    if not isinstance(scope, list):
        _err(errors, "approvals.pre_approved_scope", "invalid", "pre_approved_scope must be a list")
        return
    for cls in scope:
        if cls not in DEFAULT_PRE_APPROVED_CLASSES:
            _err(
                errors,
                "approvals.pre_approved_scope",
                "invalid_class",
                f"tool class {cls!r} is not eligible for unattended approval "
                f"(allowed: {DEFAULT_PRE_APPROVED_CLASSES})",
            )
            break
    ttl = approvals.get("approval_ttl_seconds", 900)
    if not isinstance(ttl, int) or ttl <= 0 or ttl > 24 * 3600:
        _err(errors, "approvals.approval_ttl_seconds", "invalid", "approval TTL must be 0..24h")


def _validate_retry(retry: dict, errors: list) -> None:
    max_attempts = retry.get("max_attempts", 2)
    if not isinstance(max_attempts, int) or max_attempts < 0 or max_attempts > MAX_RETRY_ATTEMPTS:
        _err(errors, "retry.max_attempts", "over_ceiling", f"max_attempts must be 0..{MAX_RETRY_ATTEMPTS}")
    base = retry.get("backoff_base_seconds", 60)
    if not isinstance(base, int) or base < 0 or base > 3600:
        _err(errors, "retry.backoff_base_seconds", "invalid", "backoff base must be 0..3600s")
    cap = retry.get("backoff_cap_seconds", 1800)
    if not isinstance(cap, int) or cap < 0 or cap > 6 * 3600:
        _err(errors, "retry.backoff_cap_seconds", "invalid", "backoff cap must be 0..6h")


def _validate_budgets(budgets: dict, errors: list) -> None:
    wall = budgets.get("wall_clock_seconds", 600)
    if not isinstance(wall, int) or wall <= 0 or wall > MAX_WALL_CLOCK_SECONDS:
        _err(errors, "budgets.wall_clock_seconds", "over_ceiling", f"wall_clock_seconds must be 0..{MAX_WALL_CLOCK_SECONDS}")
    tools = budgets.get("max_tool_calls", 60)
    if not isinstance(tools, int) or tools <= 0 or tools > MAX_TOOL_CALLS:
        _err(errors, "budgets.max_tool_calls", "over_ceiling", f"max_tool_calls must be 0..{MAX_TOOL_CALLS}")
    llm = budgets.get("max_llm_calls", 40)
    if not isinstance(llm, int) or llm <= 0 or llm > MAX_LLM_CALLS:
        _err(errors, "budgets.max_llm_calls", "over_ceiling", f"max_llm_calls must be 0..{MAX_LLM_CALLS}")


def validate_spec(spec: dict, db: Optional[Any] = None) -> dict:
    """Validate a canonical AutomationSpec. Returns {"ok": bool, "errors": [...]}.

    ``db`` (optional Session) enables existence checks: project_id and
    agent_id are verified against the database when supplied.
    """
    errors: list = []
    if not isinstance(spec, dict):
        return {"ok": False, "errors": [{"field": "spec", "code": "invalid", "message": "spec must be an object"}]}

    # ── name ────────────────────────────────────────────────────────────
    name = spec.get("name")
    if not name or not str(name).strip():
        _err(errors, "name", "required", "name is required")
    elif len(str(name)) > MAX_NAME_LENGTH:
        _err(errors, "name", "too_long", f"name must be ≤ {MAX_NAME_LENGTH} characters")

    # ── project ─────────────────────────────────────────────────────────
    project_id = spec.get("project_id")
    if not project_id:
        _err(errors, "project_id", "required", "project_id is required")
    elif db is not None:
        from app.models.project import Project

        if db.query(Project).filter(Project.id == project_id).first() is None:
            _err(errors, "project_id", "not_found", f"project {project_id!r} does not exist")

    # ── agent binding ───────────────────────────────────────────────────
    agent = spec.get("agent") or {}
    agent_id = agent.get("agent_id")
    if agent_id and db is not None:
        from app.models.agent_app import AgentApp

        row = (
            db.query(AgentApp)
            .filter(AgentApp.id == agent_id, AgentApp.is_deleted == False)  # noqa: E712
            .first()
        )
        if row is None:
            _err(errors, "agent.agent_id", "not_found", f"agent {agent_id!r} does not exist or is deleted")

    # ── sections ────────────────────────────────────────────────────────
    trigger = spec.get("trigger") or {}
    if not isinstance(trigger, dict):
        _err(errors, "trigger", "invalid", "trigger must be an object")
    else:
        _validate_trigger(trigger, errors)

    output = spec.get("output") or {}
    if not isinstance(output, dict):
        _err(errors, "output", "invalid", "output must be an object")
    else:
        _validate_output(output, errors)

    delivery = spec.get("delivery") or {}
    if not isinstance(delivery, dict):
        _err(errors, "delivery", "invalid", "delivery must be an object")
    else:
        _validate_delivery(delivery, errors)

    notifications = spec.get("notifications") or {}
    if not isinstance(notifications, dict):
        _err(errors, "notifications", "invalid", "notifications must be an object")
    else:
        _validate_notifications(notifications, errors)

    approvals = spec.get("approvals") or {}
    if not isinstance(approvals, dict):
        _err(errors, "approvals", "invalid", "approvals must be an object")
    else:
        _validate_approvals(approvals, errors)

    retry = spec.get("retry") or {}
    if not isinstance(retry, dict):
        _err(errors, "retry", "invalid", "retry must be an object")
    else:
        _validate_retry(retry, errors)

    budgets = spec.get("budgets") or {}
    if not isinstance(budgets, dict):
        _err(errors, "budgets", "invalid", "budgets must be an object")
    else:
        _validate_budgets(budgets, errors)

    # ── datasource (advisory) ───────────────────────────────────────────
    # Lifecycle separation (2026-11-03): a missing datasource is a WARNING,
    # never a silent auto-pick. The user (or the agent via clarify) binds an
    # authorized datasource before activation; execution without one must
    # produce a clarification request, not an arbitrary project source.
    warnings: list = []
    ds = spec.get("datasource") or {}
    has_datasource = isinstance(ds, dict) and any(
        ds.get(k) for k in ("data_source_id", "kb_id", "kb_ids")
    )
    if not has_datasource:
        warnings.append({
            "field": "datasource",
            "code": "datasource_not_configured",
            "message": (
                "No datasource is bound to this automation. Ask the user "
                "which authorized data source to use — never pick one "
                "automatically."
            ),
        })

    return {"ok": not errors, "errors": errors, "warnings": warnings}
