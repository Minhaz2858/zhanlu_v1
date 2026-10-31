"""erp_submit_po — governed ERP write-back (EBOM Workstream C).

Submits an ALREADY-APPROVED PO draft to a connector-neutral ERP adapter.
Runs EXCLUSIVELY through the ontology action's execute-approved path: the
tool refuses direct LLM calls (the approval id in context is the proof),
validates the draft org-side, enforces idempotency at the ledger layer,
and records the external transaction reference + audit trail.

Lifecycle: proposed → awaiting_approval → approved → executing →
succeeded | failed. Retry after failure re-enters the SAME idempotency
key and is safe; a succeeded submission is never executed twice.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import uuid4

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


def _idempotency_key(approval_id: str | None, draft_id: str,
                     connector_id: str) -> str:
    raw = f"{approval_id}|{draft_id}|{connector_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


async def _submit(args: dict, db: Session, user_id: str | None,
                  context: dict | None = None) -> dict:
    from app.models.audit_event import AuditEvent
    from app.models.erp import ErpSubmission
    from app.models.procurement import ProcurementPoDraft
    from app.services.ontology.erp import get_adapter

    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    approval_id = ctx.get("approval_id")

    draft_id = str(args.get("draft_id") or "").strip()
    if not draft_id:
        return {"success": False, "reason": "draft_id is required"}
    connector_id = str(args.get("connector_id") or "sandbox").strip()

    draft = (
        db.query(ProcurementPoDraft)
        .filter(
            ProcurementPoDraft.id == draft_id,
            ProcurementPoDraft.org_id == org_id,
            ProcurementPoDraft.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not draft:
        return {"success": False,
                "reason": f"PO draft not found in this org: {draft_id}"}

    idem = _idempotency_key(approval_id, draft_id, connector_id)
    row = (
        db.query(ErpSubmission)
        .filter(
            ErpSubmission.org_id == org_id,
            ErpSubmission.idempotency_key == idem,
        )
        .first()
    )
    if row and row.status == "succeeded":
        # Idempotent replay: the external system already has this order.
        return {"success": True, "external_ref": row.external_ref,
                "status": "succeeded", "idempotent_replay": True,
                "submission_id": row.id,
                "note": "already submitted — no duplicate ERP write"}

    if not row:
        row = ErpSubmission(
            id=str(uuid4()), org_id=org_id, draft_id=draft_id,
            connector_id=connector_id, idempotency_key=idem,
            status="executing", approval_id=approval_id,
            request_json={
                "supplier_name": draft.supplier_name,
                "product": draft.product,
                "quantity": draft.quantity, "unit": draft.unit,
                "est_unit_price": draft.est_unit_price,
                "est_amount": draft.est_amount, "currency": draft.currency,
            },
        )
        db.add(row)
    else:
        row.status = "executing"
        row.error_message = None
    db.commit()

    payload = {
        "draft_id": draft_id, "supplier_name": draft.supplier_name,
        "product": draft.product, "quantity": draft.quantity,
        "unit": draft.unit, "est_amount": draft.est_amount,
        "currency": draft.currency, "org_id": org_id,
    }
    try:
        result = get_adapter(connector_id).submit_purchase_order(payload)
    except Exception as exc:  # noqa: BLE001 — honest failure, retryable
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        db.add(AuditEvent(
            org_id=org_id, event_type="erp_submission_failed",
            event_source="ontology", actor_type="tool", actor_id=user_id,
            resource_type="erp_submission", resource_id=row.id,
            outcome="error", error_message=str(exc)[:500],
            detail_json={"connector_id": connector_id, "draft_id": draft_id},
            occurred_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc),
        ))
        db.commit()
        return {"success": False,
                "reason": f"ERP submission failed: {exc}",
                "submission_id": row.id}

    row.status = "succeeded"
    row.external_ref = result.get("external_ref")
    row.response_json = result
    db.add(AuditEvent(
        org_id=org_id, event_type="erp_submission_succeeded",
        event_source="ontology", actor_type="tool", actor_id=user_id,
        resource_type="erp_submission", resource_id=row.id,
        outcome="success",
        detail_json={"connector_id": connector_id, "draft_id": draft_id,
                     "external_ref": row.external_ref,
                     "approval_id": approval_id},
        occurred_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
    ))
    db.commit()
    logger.info("ERP submission %s succeeded (draft=%s, connector=%s)",
                row.id, draft_id, connector_id)
    return {"success": True, "external_ref": row.external_ref,
            "status": "succeeded", "submission_id": row.id,
            "connector_id": connector_id}


ERP_SUBMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "erp_submit_po",
        "description": (
            "Submit an approved PO draft to the ERP through the governed "
            "connector (write-back). Only callable through an APPROVED "
            "ontology action — direct calls are refused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string",
                             "description": "Approved PO draft id."},
                "connector_id": {"type": "string",
                                 "description": "ERP connector (default sandbox)."},
            },
            "required": ["draft_id"],
        },
    },
}


async def _direct_call_guard(args: dict, db: Session, user_id: str | None,
                             context: dict | None = None) -> dict:
    ctx = context or {}
    if not ctx.get("approval_id"):
        return {
            "success": False,
            "reason": (
                "submitting to the ERP is a WRITE action — it requires a "
                "governance approval. Route it through the ontology action "
                "'erp_submit_po' (execute → admin approves → execute-approved)."
            ),
        }
    return await _submit(args, db, user_id, context)


registry.register(
    name="erp_submit_po",
    schema=ERP_SUBMIT_SCHEMA,
    handler=_direct_call_guard,
    category="data",
    toolset="data",
    description="Approval-gated ERP purchase-order submission (write-back).",
    emoji="🏭",
    max_result_size_chars=20_000,
)
