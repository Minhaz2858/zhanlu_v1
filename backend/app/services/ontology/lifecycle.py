"""Decision lifecycle chain (EBOM Workstream D) — read-only assembler.

Answers the CEO question from EXISTING records only:
  what happened → why concerned → who approved → was it executed →
  what actually changed → was it verified.

Anchors: an escalation, an approval, a PO draft, or an outcome measurement
id. The assembler follows the references those records already carry
(approval_id, draft_id, escalation_id, context_json.action_id). Missing
stages are returned as null with the chain marking them absent — never
fabricated. Every query is org-scoped; foreign-org refs are invisible.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.escalation import Escalation
from app.models.erp import ErpSubmission
from app.models.governance import ApprovalRequest
from app.models.outcome import OutcomeMeasurement
from app.models.procurement import ProcurementPoDraft

STAGE_ORDER = ["problem", "approval", "execution", "measurement", "verification"]


def decision_lifecycle(db: Session, org_id: str, decision_ref: str) -> dict:
    ref = (decision_ref or "").strip()
    if not ref:
        return {"success": False, "reason": "decision_ref is required"}

    esc = db.query(Escalation).filter(
        Escalation.id == ref, Escalation.org_id == org_id,
        Escalation.is_deleted == False).first()  # noqa: E712
    appr = db.query(ApprovalRequest).filter(
        ApprovalRequest.id == ref, ApprovalRequest.org_id == org_id,
        ApprovalRequest.is_deleted == False).first()  # noqa: E712
    draft = db.query(ProcurementPoDraft).filter(
        ProcurementPoDraft.id == ref, ProcurementPoDraft.org_id == org_id,
        ProcurementPoDraft.is_deleted == False).first()  # noqa: E712
    outcome = db.query(OutcomeMeasurement).filter(
        OutcomeMeasurement.id == ref, OutcomeMeasurement.org_id == org_id,
        OutcomeMeasurement.is_deleted == False).first()  # noqa: E712

    if not any([esc, appr, draft, outcome]):
        return {"success": False, "not_found": True,
                "reason": f"no decision record found for {ref!r} in this org"}

    # --- follow the references the records already carry --------------------
    if appr and not draft:
        draft = db.query(ProcurementPoDraft).filter(
            ProcurementPoDraft.approval_id == appr.id,
            ProcurementPoDraft.org_id == org_id,
            ProcurementPoDraft.is_deleted == False).first()  # noqa: E712
    if draft and not appr and draft.approval_id:
        appr = db.query(ApprovalRequest).filter(
            ApprovalRequest.id == draft.approval_id,
            ApprovalRequest.org_id == org_id,
            ApprovalRequest.is_deleted == False).first()  # noqa: E712
    if not outcome and (appr or draft or esc):
        q = db.query(OutcomeMeasurement).filter(
            OutcomeMeasurement.org_id == org_id,
            OutcomeMeasurement.is_deleted == False)  # noqa: E712
        conds = []
        if appr:
            conds.append(OutcomeMeasurement.approval_id == appr.id)
        if draft:
            conds.append(OutcomeMeasurement.draft_id == draft.id)
        if esc:
            conds.append(OutcomeMeasurement.escalation_id == esc.id)
        from sqlalchemy import or_
        outcome = q.filter(or_(*conds)).order_by(
            OutcomeMeasurement.created_date.desc()).first()
    if outcome and not esc and outcome.escalation_id:
        esc = db.query(Escalation).filter(
            Escalation.id == outcome.escalation_id,
            Escalation.org_id == org_id,
            Escalation.is_deleted == False).first()  # noqa: E712
    if outcome and not appr and outcome.approval_id:
        appr = db.query(ApprovalRequest).filter(
            ApprovalRequest.id == outcome.approval_id,
            ApprovalRequest.org_id == org_id,
            ApprovalRequest.is_deleted == False).first()  # noqa: E712
    if outcome and not draft and outcome.draft_id:
        draft = db.query(ProcurementPoDraft).filter(
            ProcurementPoDraft.id == outcome.draft_id,
            ProcurementPoDraft.org_id == org_id,
            ProcurementPoDraft.is_deleted == False).first()  # noqa: E712
    if appr and not esc:
        esc_ref = (appr.context_json or {}).get("escalation_id")
        if esc_ref:
            esc = db.query(Escalation).filter(
                Escalation.id == esc_ref, Escalation.org_id == org_id,
                Escalation.is_deleted == False).first()  # noqa: E712

    # --- stages --------------------------------------------------------------
    problem = None
    if esc:
        problem = {"escalation_id": esc.id, "summary": esc.summary,
                   "status": esc.status, "sentiment": esc.sentiment}

    approval = None
    if appr:
        ctx = appr.context_json or {}
        approval = {
            "approval_id": appr.id, "status": appr.status,
            "reviewed_by": appr.reviewed_by, "action_id": ctx.get("action_id"),
            "tool_name": ctx.get("tool_name"), "params": ctx.get("params"),
        }

    execution = None
    submissions: list[ErpSubmission] = []
    if draft:
        submissions = (db.query(ErpSubmission)
                       .filter(ErpSubmission.org_id == org_id,
                               ErpSubmission.draft_id == draft.id)
                       .order_by(ErpSubmission.created_date.asc()).all())
        execution = {
            "draft": {
                "id": draft.id, "supplier_name": draft.supplier_name,
                "product": draft.product, "quantity": draft.quantity,
                "unit": draft.unit, "est_amount": draft.est_amount,
                "currency": draft.currency, "status": draft.status,
            },
            "erp_submissions": [
                {"id": x.id, "connector_id": x.connector_id,
                 "status": x.status, "external_ref": x.external_ref}
                for x in submissions
            ],
        }

    measurement = None
    verification = None
    if outcome:
        measurement = {
            "outcome_id": outcome.id, "metric_name": outcome.metric_name,
            "unit": outcome.unit, "direction": outcome.direction,
            "expected_impact": outcome.expected_impact,
            "baseline_value": outcome.baseline_value,
            "observed_value": outcome.observed_value,
            "realized_impact": outcome.realized_impact,
            "measurement_status": outcome.measurement_status,
            "baseline_period": outcome.baseline_period,
            "observed_period": outcome.observed_period,
        }
        if outcome.verification_status == "verified":
            verification = {
                "verified_by": outcome.verified_by,
                "verified_at": outcome.verified_at.isoformat()
                if outcome.verified_at else None,
                "note": outcome.verification_note,
                "status": "verified",
            }

    # Execution decision audits (the resolve + ERP submission trail).
    decision_audits = []
    audit_ids = [x.id for x in submissions]
    if esc:
        audit_ids.append(esc.id)
    if audit_ids:
        rows = (db.query(AuditEvent)
                .filter(AuditEvent.org_id == org_id,
                        AuditEvent.resource_id.in_(audit_ids))
                .order_by(AuditEvent.occurred_at.asc()).limit(50).all())
        decision_audits = [
            {"event_type": a.event_type, "actor_id": a.actor_id,
             "outcome": a.outcome, "occurred_at": a.occurred_at.isoformat()
             if a.occurred_at else None}
            for a in rows
        ]
    if execution is not None:
        execution["decision_audits"] = decision_audits

    stages = {"problem": problem, "approval": approval,
              "execution": execution, "measurement": measurement,
              "verification": verification}
    chain = [[stage, stages[stage] is not None] for stage in STAGE_ORDER]
    return {"success": True, "anchor": ref, "stages": stages, "chain": chain}
