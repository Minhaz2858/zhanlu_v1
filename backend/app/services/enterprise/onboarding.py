"""Resource onboarding & continuous ontology evolution (Phase 4).

Deterministic, bounded, org-scoped lifecycle over the EXISTING discovery
infrastructure (``enterprise_discovery.refresh_inventory`` /
``extract_evidence`` / ``propose_operating_model``) — no second engine.

Capability map (each type only does what its integration supports):
  datasource / knowledge_base → inventory + evidence + discovery
  mcp_server / automation     → inventory only (honestly skipped steps)

Idempotency: a stable inventory digest short-circuits re-onboarding (no
duplicate discovery). Drift (digest change) reprocesses: new evidence/
proposals are added, existing evidence is NEVER deleted, removed catalog
tables are recorded as stale references. Proposals stay PROPOSED; process
statuses are never auto-promoted. Every run audited.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

from app.models.enterprise_onboarding import EnterpriseOnboardingRun

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = ("datasource", "knowledge_base", "mcp_server",
                   "automation")
_EVIDENCE_TYPES = ("datasource", "knowledge_base")
_MAX_CATALOG_NAMES = 200


def _digest(names) -> str:
    return hashlib.sha256(
        "|".join(sorted({str(n).lower() for n in names})).encode()
    ).hexdigest()[:64]


def _resource_digest(db: Session, org_id: str, resource_type: str,
                     resource_id: str) -> str | None:
    """Digest of the resource's catalog inventory (drift signal).
    NULL = not computable for this type (honest: no drift detection)."""
    from app.models.knowledge_catalog import KBTableMeta

    if resource_type not in _EVIDENCE_TYPES:
        return None
    rows = db.query(KBTableMeta.table_name).filter(
        KBTableMeta.kb_id == resource_id,
        KBTableMeta.is_deleted == False,  # noqa: E712
    ).all()
    return _digest(r[0] for r in rows)


def _latest_run(db: Session, org_id: str, resource_type: str,
                resource_id: str) -> EnterpriseOnboardingRun | None:
    return db.query(EnterpriseOnboardingRun).filter(
        EnterpriseOnboardingRun.org_id == org_id,
        EnterpriseOnboardingRun.resource_type == resource_type,
        EnterpriseOnboardingRun.resource_id == resource_id,
        EnterpriseOnboardingRun.is_deleted == False,  # noqa: E712
    ).order_by(EnterpriseOnboardingRun.created_date.desc()).first()


def _prev_catalog_tables(run: EnterpriseOnboardingRun | None) -> set:
    """Catalog table names recorded on the previous run (drift diff)."""
    if not run or not run.steps_json:
        return set()
    for s in run.steps_json:
        if s.get("step") == "catalog":
            return set((s.get("detail") or {}).get("tables") or [])
    return set()


def _resource_exists(db: Session, org_id: str, resource_type: str,
                     resource_id: str) -> bool:
    """Cheap existence/authorization check BEFORE the digest short-circuit
    (a deleted resource must never be 'skipped_unchanged')."""
    if resource_type not in _EVIDENCE_TYPES:
        return True  # automation/mcp: inventory-only, verified at step 1
    from app.models.knowledge_base import KnowledgeBase

    return db.query(KnowledgeBase.id).filter(
        KnowledgeBase.id == resource_id,
        KnowledgeBase.org_id == org_id,
        KnowledgeBase.is_deleted == False,  # noqa: E712
    ).first() is not None


def _resource_in_inventory(inv: dict, resource_type: str,
                           resource_id: str) -> dict | None:
    section = {"datasource": "datasources",
               "knowledge_base": "documents"}.get(resource_type)
    if not section:
        return {"id": resource_id}  # automation/mcp: accepted as-is
    for item in inv.get(section) or []:
        if item.get("id") == resource_id:
            return item
    return None


def _finish_run(db, org_id, resource_type, resource_id, item, steps, status,
                error, digest, attempt, actor_id) -> EnterpriseOnboardingRun:
    run = EnterpriseOnboardingRun(
        org_id=org_id, resource_type=resource_type,
        resource_id=resource_id,
        resource_name=(item or {}).get("name"),
        status=status, steps_json=steps, inventory_digest=digest,
        attempt=attempt, error=error, actor_id=actor_id)
    db.add(run)
    _audit_run(db, org_id, run)
    db.commit()
    return run


def _audit_run(db: Session, org_id: str, run: EnterpriseOnboardingRun) -> None:
    """Audit every run via the existing AuditEvent mechanism. Fail-safe."""
    from datetime import datetime, timezone

    from app.models.audit_event import AuditEvent

    try:
        db.add(AuditEvent(
            org_id=org_id,
            event_type="resource_onboarded",
            event_source="ontology",
            actor_type="user" if run.actor_id else "system",
            actor_id=run.actor_id,
            resource_type="enterprise_resource",
            resource_id=run.resource_id,
            outcome=("completed" if run.status == "completed"
                     else "denied" if run.status == "failed" else "partial"),
            detail_json={
                "resource_type": run.resource_type,
                "resource_id": run.resource_id,
                "status": run.status,
                "attempt": run.attempt,
                "steps": run.steps_json,
                "inventory_digest": run.inventory_digest,
            },
            occurred_at=datetime.now(timezone.utc),
        ))
    except Exception as e:  # noqa: BLE001 — audit never blocks the run
        logger.warning("onboarding audit failed (run stands): %s", e)


def onboard_resource(
    db: Session, org_id: str, resource_type: str, resource_id: str,
    *, actor_id: str | None = None, retry: bool = False, llm_fn=None,
) -> dict:
    """Onboard (or re-onboard after drift) one authorized resource.

    Bounded per-step failure isolation; org-scoped; idempotent via the
    inventory digest. Returns the run decision; the run row is the
    trace/audit surface.
    """
    from app.services.ontology import enterprise_discovery as ed

    if resource_type not in SUPPORTED_TYPES:
        return {"status": "failed",
                "error": f"unsupported resource type {resource_type!r}",
                "steps": []}

    last = _latest_run(db, org_id, resource_type, resource_id)
    digest = _resource_digest(db, org_id, resource_type, resource_id)

    if not _resource_exists(db, org_id, resource_type, resource_id):
        steps = [{"step": "authorization", "status": "failed",
                  "detail": "resource not found in this organization"}]
        run = _finish_run(db, org_id, resource_type, resource_id, None,
                          steps, "failed", steps[0]["detail"], digest, 1,
                          actor_id)
        return {"status": "failed", "steps": steps,
                "error": steps[0]["detail"], "run_id": run.id}

    if (last is not None and last.status == "completed" and not retry
            and digest is not None and last.inventory_digest == digest):
        run = EnterpriseOnboardingRun(
            org_id=org_id, resource_type=resource_type,
            resource_id=resource_id, status="skipped_unchanged",
            steps_json=[{"step": "all", "status": "skipped",
                         "detail": "inventory digest unchanged"}],
            inventory_digest=digest,
            attempt=(last.attempt or 1) + 1, actor_id=actor_id)
        db.add(run)
        db.commit()
        return {"status": "skipped_unchanged", "steps": run.steps_json,
                "attempt": run.attempt, "run_id": run.id}

    steps: list[dict] = []
    error = None
    status = "completed"
    item = None
    inv: dict = {}

    # ── authorization + inventory registration ─────────────────────────
    try:
        inv = ed.refresh_inventory(db, org_id)
        item = _resource_in_inventory(inv, resource_type, resource_id)
        if item is None:
            steps.append({"step": "authorization", "status": "failed",
                          "detail": "resource not found in org inventory"})
            run = _finish_run(db, org_id, resource_type, resource_id, None,
                              steps, "failed",
                              "resource not found in org inventory",
                              digest, 1, actor_id)
            return {"status": "failed", "steps": steps,
                    "error": steps[-1]["detail"], "run_id": run.id}
        steps.append({"step": "inventory", "status": "ok",
                      "detail": f"{resource_type}:{resource_id} registered"})
    except Exception as e:  # noqa: BLE001 — per-step isolation
        steps.append({"step": "inventory", "status": "failed",
                      "detail": str(e)[:300]})
        status, error = "failed", str(e)[:300]

    attempt = ((last.attempt or 1) + 1) if (retry and last) else 1

    # ── catalog snapshot (drift diff surface) ───────────────────────────
    if resource_type in _EVIDENCE_TYPES:
        from app.models.knowledge_catalog import KBTableMeta

        current = [r[0] for r in db.query(KBTableMeta.table_name).filter(
            KBTableMeta.kb_id == resource_id,
            KBTableMeta.is_deleted == False).all()]  # noqa: E712
        steps.append({"step": "catalog", "status": "ok",
                      "detail": {"tables": current[:_MAX_CATALOG_NAMES]}})
        removed = sorted(_prev_catalog_tables(last) - set(current)) \
            if last else []
        if removed:
            steps.append({
                "step": "stale_references", "status": "ok",
                "detail": {"removed_tables": removed,
                           "note": "evidence preserved; references "
                                   "recorded stale, never deleted"}})

    # ── evidence extraction (only what the integration supports) ────────
    if resource_type in _EVIDENCE_TYPES and status != "failed":
        try:
            evidence, skipped = ed.extract_evidence(db, org_id, inv)
            steps.append({"step": "evidence", "status": "ok",
                          "detail": {"evidence_count": len(evidence),
                                     "skipped": len(skipped or [])}})
        except Exception as e:  # noqa: BLE001
            steps.append({"step": "evidence", "status": "failed",
                          "detail": str(e)[:300]})
            status, error = "partial_failed", str(e)[:300]
    elif status != "failed":
        steps.append({"step": "evidence", "status": "skipped",
                      "detail": "unsupported: resource integration "
                                "provides no extractable evidence"})

    # ── discovery → PROPOSALS (never verified) ──────────────────────────
    if resource_type in _EVIDENCE_TYPES and status != "failed":
        try:
            proposal = ed.propose_operating_model(db, org_id, llm_fn=llm_fn)
            steps.append({"step": "discovery", "status": "ok",
                          "detail": {"llm_ok": bool(proposal.get("llm_ok")),
                                     "proposals": len(
                                         proposal.get("proposals") or []),
                                     "note": "all proposals remain "
                                             "PROPOSED"}})
        except Exception as e:  # noqa: BLE001
            steps.append({"step": "discovery", "status": "failed",
                          "detail": str(e)[:300]})
            if status == "completed":
                status = "partial_failed"
            error = error or str(e)[:300]
    elif status != "failed":
        steps.append({"step": "discovery", "status": "skipped",
                      "detail": "unsupported for this resource type"})

    run = _finish_run(db, org_id, resource_type, resource_id, item, steps,
                      status, error, digest, attempt, actor_id)
    return {"status": status, "steps": steps, "error": error,
            "attempt": run.attempt, "run_id": run.id}


def on_resource_changed(
    db: Session, org_id: str, resource_type: str, resource_id: str,
    *, actor_id: str | None = None,
) -> dict:
    """Evolution entry point: detect catalog drift via the inventory
    digest and reprocess when it changed. Verified knowledge is never
    deleted — removed tables are recorded as stale references."""
    last = _latest_run(db, org_id, resource_type, resource_id)
    digest = _resource_digest(db, org_id, resource_type, resource_id)
    drifted = bool(
        digest is not None and last is not None
        and last.inventory_digest not in (None, digest))
    run = onboard_resource(db, org_id, resource_type, resource_id,
                           actor_id=actor_id, retry=drifted)
    # an unchanged resource is a no-op evolution, not a failure
    if not drifted and run["status"] == "skipped_unchanged":
        return {"drift_detected": False, "run": run}
    return {"drift_detected": drifted, "run": run}


def get_onboarding_status(db: Session, org_id: str, resource_type: str,
                          resource_id: str) -> dict | None:
    """Lifecycle status for one resource (org-scoped; None cross-org)."""
    runs = db.query(EnterpriseOnboardingRun).filter(
        EnterpriseOnboardingRun.org_id == org_id,
        EnterpriseOnboardingRun.resource_type == resource_type,
        EnterpriseOnboardingRun.resource_id == resource_id,
        EnterpriseOnboardingRun.is_deleted == False,  # noqa: E712
    ).order_by(EnterpriseOnboardingRun.created_date.desc()).all()
    if not runs:
        return None
    latest = runs[0]
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "latest": {"status": latest.status, "steps": latest.steps_json,
                   "attempt": latest.attempt, "error": latest.error,
                   "inventory_digest": latest.inventory_digest,
                   "updated_date": latest.updated_date.isoformat()
                   if latest.updated_date else None},
        "run_count": len(runs),
    }
