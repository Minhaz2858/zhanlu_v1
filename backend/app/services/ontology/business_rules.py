"""BusinessRule governance & review lifecycle (Phase 1B.1, 2026-11-01).

Human review for the unified BusinessRule model. Follows the same
conventions as the EBPM/ontology review machinery
(`enterprise_discovery._record_review`): org-scoped fail-closed loads,
append-only review history (via ``BusinessRule.record_review``), one
AuditEvent per decision, and decisions that only ever move the rule
forward — rediscovery (Phase 1B.2+) must never weaken a human decision.

Transition contract (enforced here, not by the DB — the DB stores the
status vocabulary; the lifecycle is procedural):

    proposed → verified      (confirm, org-admin)
    proposed → rejected      (reject, org-admin)
    verified → enabled       (enable, org-admin)
    enabled  → verified      (disable, org-admin)
    rejected → proposed      (resubmit — explicit resubmission only)

``proposed → enabled`` is NEVER allowed directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.business_rule import APPLIES_TO, SCOPES, BusinessRule

# action → {current_status: next_status}
TRANSITIONS: dict[str, dict[str, str]] = {
    "confirm": {"proposed": "verified"},
    "reject": {"proposed": "rejected"},
    "enable": {"verified": "enabled"},
    "disable": {"enabled": "verified"},
    "resubmit": {"rejected": "proposed"},
}

_DECISIONS = {
    "confirm": "confirmed",
    "reject": "rejected",
    "enable": "enabled",
    "disable": "disabled",
    "resubmit": "resubmitted",
}


class RuleLifecycleError(Exception):
    """Invalid lifecycle request; carries the HTTP status to raise."""

    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _load_rule(db: Session, org_id: str, rule_id: str) -> BusinessRule | None:
    """Org-scoped, fail-closed load. Cross-org or missing → None (404)."""
    return db.query(BusinessRule).filter(
        BusinessRule.org_id == org_id,
        BusinessRule.id == rule_id,
        BusinessRule.is_deleted == False,  # noqa: E712
    ).first()


def _validate_shape(rule: BusinessRule) -> None:
    """Defensive shape validation (CheckConstraints also guard the DB):
    vocabulary membership + scoped rules must name a target."""
    if rule.scope not in SCOPES:
        raise RuleLifecycleError(
            f"Invalid scope {rule.scope!r}", status_code=422)
    if rule.applies_to not in APPLIES_TO:
        raise RuleLifecycleError(
            f"Invalid applies_to {rule.applies_to!r}", status_code=422)
    if rule.scope != "org" and not rule.scope_ref_id:
        raise RuleLifecycleError(
            f"Scoped rule ({rule.scope}) must reference a target "
            "(scope_ref_id)", status_code=422)
    if not (rule.statement or "").strip():
        raise RuleLifecycleError("Rule statement must not be empty",
                                 status_code=422)


def _audit(db: Session, org_id: str, rule: BusinessRule, action: str,
           from_status: str, to_status: str, reviewer, reason: str | None):
    name = (getattr(reviewer, "full_name", None)
            or getattr(reviewer, "email", None))
    db.add(AuditEvent(
        org_id=org_id,
        event_type=f"business_rule_{_DECISIONS[action]}",
        event_source="ontology",
        actor_type="user",
        actor_id=getattr(reviewer, "id", None),
        actor_name=name,
        resource_type="business_rule",
        resource_id=rule.id,
        outcome="success",
        detail_json={
            "scope": rule.scope,
            "scope_ref_id": rule.scope_ref_id,
            "applies_to": rule.applies_to,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "statement": (rule.statement or "")[:300],
        },
        occurred_at=datetime.now(timezone.utc),
    ))


def review_business_rule(db: Session, org_id: str, rule_id: str, action: str,
                         reviewer, reason: str | None = None) -> dict | None:
    """Apply a lifecycle action (org-admin caller). Returns the serialized
    rule, None if missing/cross-org (404), raises RuleLifecycleError on
    invalid transitions (409) or malformed rules (422)."""
    if action not in TRANSITIONS:
        raise RuleLifecycleError(f"Unknown action {action!r}",
                                 status_code=422)
    rule = _load_rule(db, org_id, rule_id)
    if rule is None:
        return None
    _validate_shape(rule)

    from_status = rule.status
    to_status = TRANSITIONS[action].get(from_status)
    if to_status is None:
        raise RuleLifecycleError(
            f"Cannot {action} a {from_status} rule "
            f"(allowed: {', '.join(sorted(TRANSITIONS[action]))})",
            status_code=409)

    rule.record_review(_DECISIONS[action],
                       reviewer_id=getattr(reviewer, "id", None),
                       reviewer_name=(getattr(reviewer, "full_name", None)
                                      or getattr(reviewer, "email", None)),
                       reason=reason)
    rule.status = to_status
    # Evidence ids are append-only input — nothing here ever touches them.
    _audit(db, org_id, rule, action, from_status, to_status, reviewer, reason)
    # Phase 1B.2: keep the SemanticGuidelines compatibility layer in step
    # (fail-open — a bridge failure never blocks a governance decision).
    from app.services.ontology.business_rule_bridge import maybe_sync_on_review

    maybe_sync_on_review(db, rule)
    db.commit()
    db.refresh(rule)
    return serialize_rule(rule)


def serialize_rule(rule: BusinessRule) -> dict:
    return {
        "id": rule.id,
        "org_id": rule.org_id,
        "scope": rule.scope,
        "scope_ref_id": rule.scope_ref_id,
        "statement": rule.statement,
        "source_evidence_ids": rule.source_evidence_ids or [],
        "status": rule.status,
        "applies_to": rule.applies_to,
        # Phase 6F: marker-protocol effect (block | approval | informational)
        # — enforcement still depends on status=='enabled'; presented as
        # metadata, never as an enforcement claim by itself.
        "effect": _rule_effect(rule.statement),
        "review_history": rule.review_history or [],
        "created_date": rule.created_date.isoformat()
        if rule.created_date else None,
        "updated_date": rule.updated_date.isoformat()
        if rule.updated_date else None,
    }


def list_business_rules(db: Session, org_id: str, *, status: str | None = None,
                        scope: str | None = None, applies_to: str | None = None,
                        page: int = 1, page_size: int = 20) -> dict:
    """Org-scoped listing with status/scope/applies_to filters and
    pagination. Evidence ids and review history are included per item."""
    q = db.query(BusinessRule).filter(
        BusinessRule.org_id == org_id,
        BusinessRule.is_deleted == False,  # noqa: E712
    )
    if status:
        q = q.filter(BusinessRule.status == status)
    if scope:
        q = q.filter(BusinessRule.scope == scope)
    if applies_to:
        q = q.filter(BusinessRule.applies_to == applies_to)
    total = q.count()
    rows = (q.order_by(BusinessRule.updated_date.desc().nullslast(),
                       BusinessRule.created_date.desc().nullslast())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {"items": [serialize_rule(r) for r in rows], "total": total,
            "page": page, "page_size": page_size}


def get_business_rule(db: Session, org_id: str, rule_id: str) -> dict | None:
    rule = _load_rule(db, org_id, rule_id)
    if rule is None:
        return None
    out = serialize_rule(rule)
    # Phase 6F: resolve evidence ids to org ProcessEvidence rows — a rule's
    # evidence trail becomes navigable. Missing/deleted ids stay listed,
    # honestly marked unavailable (never dropped, never invented).
    from app.models.enterprise_process import ProcessEvidence

    ids = rule.source_evidence_ids or []
    rows: dict = {}
    if ids:
        for e in db.query(ProcessEvidence).filter(
                ProcessEvidence.org_id == org_id,
                ProcessEvidence.id.in_(ids)).all():
            rows[e.id] = e
    out["resolved_evidence"] = [{
        "id": rid,
        "available": rid in rows,
        **({"source_ref": rows[rid].source_ref,
            "evidence_kind": rows[rid].evidence_kind,
            "evidence_status": rows[rid].evidence_status,
            "process_id": rows[rid].process_id}
           if rid in rows else {}),
    } for rid in ids]
    return out


# ── Phase 3D.2 — governed-action pre-checks (deterministic, no LLM) ─────────
#
# Enabled ``applies_to='action'`` rules enforce at the ontology action
# execution boundary. Determinism contract: the decision comes from a
# statement MARKER protocol, never from model interpretation —
#   ``PROHIBIT:`` / ``FORBIDDEN:`` / ``DENY:``            → BLOCK
#   ``REQUIRE_APPROVAL:`` / ``APPROVAL_REQUIRED:``        → REQUIRE_APPROVAL
#   (case-insensitive, colon required)                    → informational
# An enabled rule WITHOUT a marker is surfaced in ``applicable_rule_ids``
# (traceable) but never gates execution — free text alone is not a
# demonstrable violation, and missing evidence is never converted into
# enforcement. Proposed/verified-not-enabled/rejected rules NEVER enforce.

_ACTION_MARKERS = {
    "prohibit": "block",
    "forbidden": "block",
    "deny": "block",
    "require_approval": "approval",
    "approval_required": "approval",
}


def _rule_effect(statement: str | None) -> str:
    head = (statement or "").strip().lower()
    for marker, effect in _ACTION_MARKERS.items():
        if head.startswith(f"{marker}:"):
            return effect
    return "informational"


# Phase 6F: public alias — presentation layers (drilldowns, rule browsers)
# surface the marker-protocol effect so users can distinguish executable
# guidance from informational knowledge without re-implementing the parse.
rule_effect = _rule_effect


def applicable_action_rules(
    db: Session, org_id: str, action, *,
    datasource_id: str | None = None,
) -> list:
    """Enabled action rules that APPLY to this action context.

    Org wall first; scope match: ``org`` always, ``object`` via
    ``action.object_id``, ``domain`` via the object's domain (one
    org-scoped lookup); ``process`` scope has no action-context linkage
    and never matches (documented limitation). ``datasource_ids`` scoped
    rules match ONLY when the caller's datasource is listed — scoping can
    only narrow, never widen.
    """
    from app.models.ontology import OntologyObject

    rows = db.query(BusinessRule).filter(
        BusinessRule.org_id == org_id,
        BusinessRule.is_deleted == False,  # noqa: E712
        BusinessRule.status == "enabled",
        BusinessRule.applies_to == "action",
    ).all()
    obj = None
    if action.object_id:
        obj = db.query(OntologyObject).filter(
            OntologyObject.id == action.object_id,
            OntologyObject.org_id == org_id,
            OntologyObject.is_deleted == False,  # noqa: E712
        ).first()
    out = []
    for r in rows:
        if r.scope == "object":
            if not action.object_id or r.scope_ref_id != action.object_id:
                continue
        elif r.scope == "domain":
            if not obj or not obj.domain_id \
                    or r.scope_ref_id != obj.domain_id:
                continue
        elif r.scope != "org":
            continue  # process scope: no action linkage (limitation)
        ds_ids = list(r.datasource_ids or [])
        if ds_ids and (not datasource_id
                       or str(datasource_id) not in {str(d) for d in ds_ids}):
            continue
        out.append(r)
    return out


def evaluate_action_rules(
    db: Session, org_id: str, action, *, params: dict | None = None,
    datasource_id: str | None = None, approval_granted: bool = False,
) -> dict:
    """Deterministic pre-check decision for one governed action.

    Returns the structured decision contract:
    ``{decision: allow|block|require_approval, applicable_rule_ids,
    violated_rule_ids, approval_required, explanation, evidence,
    evaluation_status}``. ``approval_granted=True`` (the approved-execution
    path) SATISFIES approval rules — but a prohibition still vetoes: an
    approval flow the rule governs cannot override the rule itself.
    """
    rules = applicable_action_rules(db, org_id, action,
                                    datasource_id=datasource_id)
    applicable_ids = [r.id for r in rules]
    violated = [r.id for r in rules if _rule_effect(r.statement) == "block"]
    needs_approval = any(_rule_effect(r.statement) == "approval" for r in rules)
    evidence = [{
        "rule_id": r.id, "scope": r.scope,
        "effect": _rule_effect(r.statement),
        "evidence_status": "enabled",
    } for r in rules]

    if violated:
        decision = "block"
        explanation = (
            f"action {action.name!r} is prohibited by enabled business "
            f"rule(s) {', '.join(violated)} — execution refused before any "
            "side effect")
    elif needs_approval and not approval_granted:
        decision = "require_approval"
        approval_rules = [r.id for r in rules
                          if _rule_effect(r.statement) == "approval"]
        explanation = (
            f"business rule(s) {', '.join(approval_rules)} require human "
            f"approval before {action.name!r} — route through the existing "
            "governance approval workflow")
    else:
        decision = "allow"
        explanation = (
            "no applicable blocking rule"
            if not applicable_ids else
            f"{len(applicable_ids)} applicable enabled rule(s) recorded; "
            "none blocking")
    return {
        "decision": decision,
        "applicable_rule_ids": applicable_ids,
        "violated_rule_ids": violated,
        "approval_required": decision == "require_approval",
        "explanation": explanation,
        "evidence": evidence,
        "evaluation_status": "deterministic",
    }


def audit_action_rule_decision(
    db: Session, org_id: str, action, decision: dict, *,
    user_id: str | None = None, context: dict | None = None,
    session_factory=None,
) -> None:
    """Failure-safe audit of one rule pre-check (existing AuditEvent
    mechanism, independent session — mirrors audit_permission_denial so a
    persistence failure can never turn a refusal into an execution)."""
    import logging
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session as _Session

    from app.models.audit_event import AuditEvent
    from app.services.ontology.permissions import resolve_caller

    logger = logging.getLogger(__name__)
    caller = resolve_caller(context)
    if session_factory is None:
        try:
            bind = db.get_bind()

            def session_factory():
                return _Session(bind=bind)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "business rule audit: no bind available (decision stands): %s",
                exc)
            return
    try:
        s = session_factory()
        try:
            s.add(AuditEvent(
                org_id=org_id,
                event_type="business_rule_check",
                event_source="ontology",
                actor_type="agent" if caller["kind"] == "agent" else caller["kind"],
                actor_id=caller["agent_name"] or caller.get("user_id") or user_id,
                resource_type="ontology_action",
                resource_id=action.id,
                outcome=("denied" if decision.get("decision") == "block"
                         else "allowed"),
                detail_json={
                    "action_name": action.name,
                    "tool_name": action.tool_name,
                    "decision": decision.get("decision"),
                    "applicable_rule_ids": decision.get("applicable_rule_ids"),
                    "violated_rule_ids": decision.get("violated_rule_ids"),
                    "approval_required": decision.get("approval_required"),
                    "evaluation_status": decision.get("evaluation_status"),
                },
                occurred_at=datetime.now(timezone.utc),
            ))
            s.commit()
        finally:
            s.close()
    except Exception as exc:  # noqa: BLE001 — audit must never mask the decision
        logger.warning(
            "business rule audit write failed (decision stands): %s", exc)
