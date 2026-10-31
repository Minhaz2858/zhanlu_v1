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
    return serialize_rule(rule) if rule else None
