"""BusinessRule → SemanticGuidelines compatibility bridge (Phase 1B.2).

BusinessRule is the AUTHORITATIVE home for enterprise business rules;
SemanticGuidelines remains the existing compatibility layer that the
NL2SQL prompt injector consumes (``SemanticStore.guidelines`` →
BUSINESS RULES block). This bridge materializes derived guideline rows —
marked ``[business-rule:{rule_id}]`` — so enabled SQL rules reach SQL
retrieval through the EXISTING engine. No second rule engine.

Invariants:
- ONLY ``status='enabled'`` AND ``applies_to='sql'`` rules are
  materialized. proposed / rejected / merely verified rules NEVER
  influence SQL (their derived rows are soft-deleted or never created).
- Manual (unmarked) SemanticGuidelines are never touched — sync only
  manages rows carrying the marker prefix.
- Idempotent: re-running produces no duplicates and no churn; rows are
  updated in place or soft-deleted back to the exact derived state.
- Provenance preserved: content carries a source footer with the rule
  id, scope and evidence ids; org/scope are preserved on the row.
- Disabling (or rejecting/resubmitting) a rule removes its derived rows
  from retrieval on the same transaction (lifecycle hook).
- Rules with ``datasource_ids`` target those KBs; rules without target
  every non-deleted KB of the org (enumerated at sync time — new KBs
  are picked up by the next sync).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.business_rule import BusinessRule

logger = logging.getLogger(__name__)

MARKER_PREFIX = "[business-rule:"


def marker_for(rule_id: str) -> str:
    return f"{MARKER_PREFIX}{rule_id}]"


def _is_marker_title(title: str | None) -> bool:
    return bool(title) and title.startswith(MARKER_PREFIX)


def _content_for(rule: BusinessRule) -> str:
    """Statement + provenance footer (authoritative source, scope,
    evidence ids) so derived rows stay traceable."""
    evidence = ",".join(rule.source_evidence_ids or [])
    return (
        f"{rule.statement}\n\n[source: business_rule:{rule.id} | "
        f"scope: {rule.scope}:{rule.scope_ref_id or '-'} | "
        f"applies_to: {rule.applies_to} | evidence: {evidence or '-'}]"
    )


def _targets(db: Session, rule: BusinessRule) -> list[str]:
    if rule.datasource_ids:
        return [str(x) for x in rule.datasource_ids]
    from app.models.knowledge_base import KnowledgeBase

    return [r.id for r in db.query(KnowledgeBase.id).filter(
        KnowledgeBase.org_id == rule.org_id,
        KnowledgeBase.is_deleted == False,  # noqa: E712
    ).all()]


def _existing_derived(db: Session, org_id: str,
                      rule_id: str) -> dict[str, object]:
    from app.models.semantic_guideline import SemanticGuideline

    rows = db.query(SemanticGuideline).filter(
        SemanticGuideline.org_id == org_id,
        SemanticGuideline.is_deleted == False,  # noqa: E712
        SemanticGuideline.title.like(f"{marker_for(rule_id)}%"),
    ).all()
    return {r.datasource_id: r for r in rows}


def sync_single_rule(db: Session, rule: BusinessRule) -> dict:
    """Materialize (or retract) the derived guidelines for ONE rule so
    its retrieval state exactly matches its lifecycle status. Idempotent;
    called from the review lifecycle and by the full sync."""
    from app.models.semantic_guideline import SemanticGuideline

    rule_id = rule.id
    existing = _existing_derived(db, rule.org_id, rule_id)
    created = updated = removed = 0

    eligible = (rule.status == "enabled"
                and rule.applies_to == "sql"
                and not rule.is_deleted)
    targets = _targets(db, rule) if eligible else []

    for ds_id in targets:
        row = existing.pop(ds_id, None)
        if row is None:
            db.add(SemanticGuideline(
                org_id=rule.org_id,
                app_id=rule.app_id,
                created_by_id=rule.created_by_id,
                datasource_id=ds_id,
                kind="other",
                title=f"{marker_for(rule_id)} {rule.scope}",
                content=_content_for(rule),
                is_enabled=True,
            ))
            created += 1
        else:
            content, title = _content_for(rule), f"{marker_for(rule_id)} {rule.scope}"
            if (row.content != content or row.title != title
                    or not row.is_enabled):
                row.content = content
                row.title = title
                row.is_enabled = True
                updated += 1
    # retract whatever remains (disabled / rejected / verified-only /
    # de-targeted datasource)
    for row in existing.values():
        row.is_deleted = True
        removed += 1
    return {"rule_id": rule_id, "created": created, "updated": updated,
            "removed": removed}


def sync_business_rules_to_guidelines(db: Session, org_id: str) -> dict:
    """Full idempotent sync for an org. Also sweeps marker rows whose
    rule no longer exists (hard-deleted externally). Manual guidelines
    are never touched."""
    from app.models.semantic_guideline import SemanticGuideline

    rules = db.query(BusinessRule).filter(
        BusinessRule.org_id == org_id,
        BusinessRule.is_deleted == False,  # noqa: E712
    ).all()
    seen, totals = set(), {"created": 0, "updated": 0, "removed": 0}
    for rule in rules:
        out = sync_single_rule(db, rule)
        seen.add(rule.id)
        for k in ("created", "updated", "removed"):
            totals[k] += out[k]

    stale = db.query(SemanticGuideline).filter(
        SemanticGuideline.org_id == org_id,
        SemanticGuideline.is_deleted == False,  # noqa: E712
        SemanticGuideline.title.like(f"{MARKER_PREFIX}%"),
    ).all()
    for row in stale:
        rid = row.title.split("]", 1)[0][len(MARKER_PREFIX):]
        if rid not in seen:
            row.is_deleted = True
            totals["removed"] += 1
    db.commit()
    return {"org_id": org_id, "rules": len(rules), **totals}


def maybe_sync_on_review(db: Session, rule: BusinessRule) -> None:
    """Fail-open lifecycle hook: keep the derived retrieval state in step
    with the reviewed rule without ever blocking a governance decision."""
    try:
        sync_single_rule(db, rule)
    except Exception as exc:  # pragma: no cover - fail-open
        logger.warning("business-rule bridge sync failed for %s: %s",
                       rule.id, exc)
