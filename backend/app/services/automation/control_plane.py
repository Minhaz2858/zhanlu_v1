"""Automation Control Plane — one authoritative creation + access path (R3).

Normative sources:
- TS §3.4 (``authorize`` policy), §4.1 (chat tool contract),
  §4.2 (replay-first confirmation transaction — normative pseudocode),
- roadmap R3 ("exactly-once creation through drafts; single access policy").

Everything here writes through the canonical models only; the LLM can
never create a definition — only drafts (invariant: the human confirm is
the only creation gate).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.automation_draft import AutomationDraft
from app.models.automation_task import AutomationTask
from app.models.audit_event import AuditEvent
from app.services.automation.spec import validate_spec

logger = logging.getLogger("zhanlu.automation.control_plane")

# Role ladder for the destructive-action policy (§3.4: default owner-only,
# configurable role override).
_EDITOR_OR_ABOVE = ("editor", "admin", "owner")
_DESTRUCTIVE_ACTIONS = ("run", "toggle", "edit", "delete", "confirm")


class ControlPlaneError(Exception):
    """Domain error with a stable machine code + details (maps to HTTP 4xx)."""

    def __init__(self, code: str, status_code: int = 409, details: Optional[dict] = None,
                 message: Optional[str] = None):
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message or code)


# ---------------------------------------------------------------------------
# §3.4 — one authorize() policy
# ---------------------------------------------------------------------------


def authorize(user: Any, task: AutomationTask, action: str) -> bool:
    """Single access-policy function for every automation action.

    - org mismatch → False (404 semantics at the API layer)
    - read → org member
    - run/toggle/edit/delete/confirm → owner OR org role ≥ editor
      (default posture; matches the entity-API's owner-only destructive
      default documented in the architecture doc §3.4).
    """
    user_org = getattr(user, "org_id", None)
    task_org = getattr(task, "org_id", None)
    if user_org and task_org and user_org != task_org:
        return False
    if action == "read":
        return True
    if action in _DESTRUCTIVE_ACTIONS:
        if getattr(task, "created_by_id", None) and task.created_by_id == getattr(user, "id", None):
            return True
        role = getattr(user, "role", "user") or "user"
        return role in _EDITOR_OR_ABOVE
    # Unknown actions are denied by default (fail-closed).
    return False


def _assert_draft_access(db: Session, user: Any, draft_id: str) -> AutomationDraft:
    draft = (
        db.query(AutomationDraft)
        .filter(AutomationDraft.id == draft_id, AutomationDraft.is_deleted == False)  # noqa: E712
        .first()
    )
    if draft is None:
        raise ControlPlaneError("not_found", status_code=404, message="Draft not found")
    user_org = getattr(user, "org_id", None)
    if user_org and draft.org_id != user_org:
        raise ControlPlaneError("not_found", status_code=404, message="Draft not found")
    if draft.created_by_id and draft.created_by_id != getattr(user, "id", None):
        role = getattr(user, "role", "user") or "user"
        if role not in _EDITOR_OR_ABOVE:
            raise ControlPlaneError("forbidden", status_code=403, message="Not the draft owner")
    return draft


# ---------------------------------------------------------------------------
# Draft CRUD (review card backs onto these — no client-only state)
# ---------------------------------------------------------------------------


def create_draft(
    db: Session,
    user: Any,
    spec_partial: dict,
    *,
    origin_session_id: Optional[str] = None,
    origin_message_id: Optional[str] = None,
) -> AutomationDraft:
    """Create a server-side draft from an LLM proposal or Center "New".

    Validation runs immediately; the result is stored on the draft so the
    review card can render inline field errors without a second call.
    """
    validation = validate_spec(spec_partial, db)
    draft = AutomationDraft(
        spec=spec_partial,
        spec_version=1,
        status="draft",
        validation_errors=validation,
        origin_session_id=origin_session_id,
        origin_message_id=origin_message_id,
        org_id=getattr(user, "org_id", None) or "default-org",
        created_by_id=getattr(user, "id", None),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def get_draft(db: Session, user: Any, draft_id: str) -> AutomationDraft:
    return _assert_draft_access(db, user, draft_id)


def patch_draft(
    db: Session, user: Any, draft_id: str, *, if_match: int, spec_patch: dict
) -> AutomationDraft:
    """Edit a draft's spec. ``If-Match`` version required; re-validates."""
    draft = _assert_draft_access(db, user, draft_id)
    if draft.status == "confirmed":
        raise ControlPlaneError(
            "already_confirmed", status_code=409, message="Confirmed drafts are immutable"
        )
    if draft.status == "discarded":
        raise ControlPlaneError(
            "draft_discarded", status_code=409, message="Draft was discarded"
        )
    if int(if_match) != draft.spec_version:
        raise ControlPlaneError(
            "stale_version", status_code=409,
            details={"current_version": draft.spec_version},
        )
    # Top-level shallow merge: a PATCH carries whole sections or scalar fields.
    merged = {**(draft.spec or {}), **(spec_patch or {})}
    draft.spec = merged
    draft.spec_version += 1
    draft.validation_errors = validate_spec(merged, db)
    db.commit()
    db.refresh(draft)
    return draft


def discard_draft(db: Session, user: Any, draft_id: str) -> AutomationDraft:
    draft = _assert_draft_access(db, user, draft_id)
    if draft.status != "confirmed":
        draft.status = "discarded"
        db.commit()
        db.refresh(draft)
    return draft


# ---------------------------------------------------------------------------
# §4.2 — replay-first confirmation transaction (normative)
# ---------------------------------------------------------------------------


def _definition_from_spec(draft: AutomationDraft, confirmation_key: str) -> AutomationTask:
    """Map the canonical spec to a definition row (scalar compat columns
    derived per TS §1.2; the spec itself is stored verbatim)."""
    spec = draft.spec or {}
    trigger = spec.get("trigger") or {}
    output = spec.get("output") or {}
    delivery = spec.get("delivery") or {}
    notifications = spec.get("notifications") or {}
    approvals = spec.get("approvals") or {}
    retry = spec.get("retry") or {}
    agent = spec.get("agent") or {}

    prompt_parts = [p for p in (spec.get("objective"), spec.get("execution_instructions")) if p]
    cron = trigger.get("cron")
    tz_name = trigger.get("timezone") or "UTC"
    next_run = None
    if trigger.get("type", "schedule") == "schedule" and cron:
        from app.services.schedule_parser import next_run_at

        try:
            next_run = next_run_at(cron, after=datetime.now(timezone.utc), tz_name=tz_name)
        except Exception:  # noqa: BLE001 — validation guarantees resolvability; stay safe
            logger.warning("confirm: next_run_at unresolved for cron %r", cron)

    # R4 trigger fidelity: explicit trigger_type + once run_at mapping.
    # A once-trigger surfaces to the legacy dispatcher via next_run_at
    # (the due query's selection key); the fire path terminalizes it.
    ttype = trigger.get("type", "schedule")
    run_at: Optional[datetime] = None
    if ttype == "once" and isinstance(trigger.get("run_at"), str):
        try:
            run_at = datetime.fromisoformat(str(trigger["run_at"]).replace("Z", "+00:00"))
            run_at = run_at.replace(tzinfo=None)  # UTC-naive storage convention
        except ValueError:
            run_at = None

    return AutomationTask(
        name=spec["name"],
        type="report",
        prompt="\n\n".join(prompt_parts) or None,
        description=spec.get("description"),
        schedule=trigger.get("cron"),
        cron_expression=cron if ttype == "schedule" else None,
        timezone=tz_name if ttype == "schedule" else None,
        next_run_at=next_run if ttype == "schedule" else run_at,
        trigger_type=ttype,
        run_at=run_at,
        status="paused",  # safe default: the user activates from the Center
        output_format=output.get("format", "html"),
        notify_chat=str(bool(delivery.get("chat", False))).lower(),
        notify_enabled=bool(delivery.get("email", False)) or bool(notifications.get("emails")),
        notify_emails=notifications.get("emails") or None,
        notify_on=notifications.get("notify_on", "on_failure"),
        max_retries=str(retry.get("max_attempts", 2)),
        skip_confirmation="false",  # replaced by pre_approved_scope (R7)
        agent_id=agent.get("agent_id"),
        project_id=spec.get("project_id"),
        spec=spec,
        spec_version=1,
        confirmation_key=confirmation_key,
        org_id=draft.org_id,
        app_id=draft.app_id,
        created_by_id=draft.created_by_id,
    )


def confirm_draft(
    db: Session,
    user: Any,
    draft_id: str,
    confirmation_key: str,
    *,
    if_match: int,
    activate: bool = False,
) -> tuple[AutomationTask, dict]:
    """Exactly-once confirm (TS §4.2). Returns ``(definition, response)``.

    ``activate`` (lifecycle separation, 2026-11-03): when True the definition
    is born ``active`` (armed schedule) INSIDE this same confirmation
    transaction — the frontend's single "Confirm & Activate" action. Default
    False keeps the safe ``paused`` default (activate later from the Center).
    Either way there are NO execution side effects here: no run, no
    occurrence, no artifact.

    Raises :class:`ControlPlaneError` with a stable code for every 4xx path.
    The whole function runs in ONE transaction on the caller's session;
    every error path leaves the draft in its pre-call state (crash-before-
    COMMIT behavior 6 is the natural transactional rollback).
    """
    if not confirmation_key or not str(confirmation_key).strip():
        raise ControlPlaneError(
            "confirmation_key_required", status_code=400,
            message="confirmation_key is required",
        )
    confirmation_key = str(confirmation_key)

    # Step 1: serialize ALL confirm paths on the draft row.
    draft = (
        db.query(AutomationDraft)
        .filter(AutomationDraft.id == draft_id)
        .with_for_update()
        .first()
    )
    if draft is None:
        raise ControlPlaneError("not_found", status_code=404, message="Draft not found")
    user_org = getattr(user, "org_id", None)
    if user_org and draft.org_id != user_org:
        raise ControlPlaneError("not_found", status_code=404, message="Draft not found")
    if draft.created_by_id and draft.created_by_id != getattr(user, "id", None):
        role = getattr(user, "role", "user") or "user"
        if role not in _EDITOR_OR_ABOVE:
            raise ControlPlaneError("forbidden", status_code=403, message="Not the draft owner")

    # Step 2: REPLAY CHECK — before any rejecting validation.
    if draft.status == "confirmed":
        if confirmation_key == draft.confirmation_key:
            # Replay ignores is_deleted (idempotency keys are immutable history).
            definition = (
                db.query(AutomationTask)
                .filter(AutomationTask.id == draft.confirmed_definition_id)
                .first()
            )
            resp: dict = {"already_confirmed": True}
            if definition is not None and definition.is_deleted:
                resp["definition_archived"] = True
            return definition, resp
        raise ControlPlaneError(
            "already_confirmed_different_key", status_code=409,
            details={"definition_id": draft.confirmed_definition_id},
            message="Draft already confirmed with a different key",
        )

    if draft.status == "discarded":
        raise ControlPlaneError(
            "draft_discarded", status_code=409, message="Draft was discarded"
        )

    # Step 3: KEY OWNERSHIP — key bound to the draft at the first attempt.
    if draft.confirmation_key is None:
        draft.confirmation_key = confirmation_key
        db.flush()
    elif draft.confirmation_key != confirmation_key:
        raise ControlPlaneError(
            "confirmation_key_mismatch", status_code=409,
            message="Draft is bound to a different confirmation key",
        )

    # Step 4: VERSION VALIDATION.
    if if_match is None or int(if_match) != draft.spec_version:
        raise ControlPlaneError(
            "stale_version", status_code=409,
            details={"current_version": draft.spec_version},
        )

    # Server-side re-validation (never trust the client's copy).
    validation = validate_spec(draft.spec, db)
    if not validation["ok"]:
        draft.validation_errors = validation
        db.commit()  # persist the refreshed errors; draft stays draft
        raise ControlPlaneError(
            "validation_failed", status_code=422, details={"errors": validation["errors"]},
            message="Specification failed validation",
        )

    # Step 5: INSERT with the DB as idempotency arbiter. IntegrityError on
    # the partial unique index means a concurrent/previous winner owns the
    # key. SQLite's pysqlite driver breaks SAVEPOINT recovery, so the loser
    # path uses a full rollback + re-bind (post-commit re-read is identical
    # on PG — behavior 5's loser observes the winner's committed row).
    task = _definition_from_spec(draft, confirmation_key)
    if activate:
        task.status = "active"  # Confirm & Activate — same transaction
    db.add(task)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Step 6: the index loser resolves the winner (re-read post-rollback).
        draft = db.query(AutomationDraft).filter(AutomationDraft.id == draft_id).first()
        winner = (
            db.query(AutomationTask)
            .filter(
                AutomationTask.org_id == draft.org_id,
                AutomationTask.confirmation_key == confirmation_key,
            )
            .first()
        )
        if winner is not None:
            draft.status = "discarded"
            draft.validation_errors = {
                "ok": False,
                "errors": [{
                    "field": "confirmation_key", "code": "already_used_by",
                    "message": f"already_used_by:{winner.id}",
                }],
            }
            db.commit()
            raise ControlPlaneError(
                "confirmation_key_already_used", status_code=409,
                details={"definition_id": winner.id},
                message="This confirmation key already produced a definition",
            )
        raise

    # Step 7: bind the draft to the definition.
    draft.status = "confirmed"
    draft.confirmed_definition_id = task.id
    draft.validation_errors = validation

    # Audit (ad-hoc writer until the R7 central writer lands).
    db.add(
        AuditEvent(
            event_type="automation_confirmed",
            event_source="automation",
            actor_type="user",
            actor_id=getattr(user, "id", None),
            resource_type="automation_task",
            resource_id=task.id,
            policy_decision="allow",
            outcome="success",
            occurred_at=datetime.now(timezone.utc),
            detail_json={"draft_id": draft.id, "confirmation_key": confirmation_key},
        )
    )
    db.commit()
    db.refresh(task)
    return task, {"already_confirmed": False}
