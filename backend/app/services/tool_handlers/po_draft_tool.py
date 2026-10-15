"""create_po_draft — the FIRST approval-gated write-back tool (P0-4).

Runs EXCLUSIVELY through the ontology action's execute-approved path
(risk_level=write): the governance approval already happened, so the
draft is born `approved` and records which approval authorized it. The
draft is a durable procurement record the buyer edits → exports → enters
in the ERP; direct ERP writes stay out of scope until a write connector
exists (honesty > theater).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _create_po_draft(args: dict, db: Session, user_id: str | None,
                           context: dict | None = None) -> dict:
    from app.models.procurement import ProcurementPoDraft

    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"

    supplier_name = str(args.get("supplier_name") or "").strip()
    if not supplier_name:
        return {"success": False,
                "reason": "supplier_name is required for a PO draft"}

    qty = args.get("quantity")
    price = args.get("est_unit_price")
    try:
        qty = float(qty) if qty is not None else None
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        return {"success": False, "reason": "quantity/est_unit_price must be numbers"}

    est_amount = args.get("est_amount")
    if est_amount is None and qty is not None and price is not None:
        est_amount = round(qty * price, 2)

    draft = ProcurementPoDraft(
        id=str(uuid4()),
        org_id=org_id,
        supplier_name=supplier_name[:255],
        supplier_code=(str(args["supplier_code"])[:64]
                       if args.get("supplier_code") else None),
        product=(str(args["product"])[:255] if args.get("product") else None),
        quantity=qty,
        unit=(str(args["unit"])[:32] if args.get("unit") else None),
        est_unit_price=price,
        est_amount=float(est_amount) if est_amount is not None else None,
        currency=(str(args["currency"])[:16] if args.get("currency") else None),
        justification=(str(args["justification"]) if args.get("justification") else None),
        status="approved",  # born approved: the tool only runs post-approval
        approval_id=ctx.get("approval_id"),
        created_by_id=user_id,
    )
    db.add(draft)
    db.commit()

    logger.info(
        "PO draft %s created for org %s (supplier=%s, approval=%s)",
        draft.id, org_id, supplier_name, ctx.get("approval_id"),
    )
    return {
        "success": True,
        "draft_id": draft.id,
        "status": draft.status,
        "approval_id": draft.approval_id,
        "supplier": draft.supplier_name,
        "est_amount": draft.est_amount,
        "note": ("draft recorded — the buyer edits, exports and enters it "
                 "in the ERP; direct ERP writes need a write connector"),
    }


CREATE_PO_DRAFT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_po_draft",
        "description": (
            "Create an approval-gated purchase-order draft (procurement "
            "write-back). Only callable through an APPROVED ontology "
            "action — direct calls by the LLM are refused unless the "
            "governance approval is in context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "supplier_name": {"type": "string"},
                "supplier_code": {"type": "string"},
                "product": {"type": "string"},
                "quantity": {"type": "number"},
                "unit": {"type": "string"},
                "est_unit_price": {"type": "number"},
                "est_amount": {"type": "number"},
                "currency": {"type": "string"},
                "justification": {"type": "string",
                                  "description": "Why this PO is needed."},
            },
            "required": ["supplier_name"],
        },
    },
}


async def _direct_call_guard(args: dict, db: Session, user_id: str | None,
                             context: dict | None = None) -> dict:
    """The LLM must NOT create PO drafts directly — only via an approved
    ontology action. The approval id in context is the proof."""
    ctx = context or {}
    if not ctx.get("approval_id"):
        return {
            "success": False,
            "reason": (
                "creating a PO draft is a WRITE action — it requires a "
                "governance approval. Route it through the ontology "
                "action 'po_draft' (execute → admin approves → "
                "execute-approved)."
            ),
        }
    return await _create_po_draft(args, db, user_id, context)


registry.register(
    name="create_po_draft",
    schema=CREATE_PO_DRAFT_SCHEMA,
    handler=_direct_call_guard,
    category="data",
    toolset="data",
    description="Approval-gated purchase-order draft (procurement write-back).",
    emoji="🧾",
    max_result_size_chars=20_000,
)
