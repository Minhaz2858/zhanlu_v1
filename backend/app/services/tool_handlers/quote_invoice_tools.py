"""P3-12 quote drafts + P3-9 invoice matching.

- create_quote_draft: SAME approval-gated pipeline as create_po_draft
  (kind='quote' on the drafts table) — the LLM cannot create quotes
  directly, only via an approved ontology action.
- match_invoice: compares an invoice against PO/contract rows — the
  THREE-WAY match, schema-agnostic: both sides are read through the
  grounded ontology path, the LLM only JUDGES the rows it is given
  (never invents), and the verdict carries the evidence.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ── P3-12: quote drafts ──────────────────────────────────────────────────

async def _create_quote_draft(args: dict, db: Session, user_id: str | None,
                              context: dict | None = None) -> dict:
    from app.models.procurement import ProcurementPoDraft

    ctx = context or {}
    org_id = ctx.get("org_id") or "default-org"
    customer_name = str(args.get("customer_name") or "").strip()
    if not customer_name:
        return {"success": False,
                "reason": "customer_name is required for a quote draft"}

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
        kind="quote",
        supplier_name=customer_name[:255],  # shared counterparty column
        product=(str(args["product"])[:255] if args.get("product") else None),
        quantity=qty,
        unit=(str(args["unit"])[:32] if args.get("unit") else None),
        est_unit_price=price,
        est_amount=float(est_amount) if est_amount is not None else None,
        currency=(str(args["currency"])[:16] if args.get("currency") else None),
        justification=(str(args["justification"]) if args.get("justification") else None),
        status="approved",  # born approved: runs only post-governance-approval
        approval_id=ctx.get("approval_id"),
        created_by_id=user_id,
    )
    db.add(draft)
    db.commit()
    logger.info("quote draft %s created for org %s (approval=%s)",
                draft.id, org_id, ctx.get("approval_id"))
    return {
        "success": True, "draft_id": draft.id, "kind": "quote",
        "status": draft.status, "approval_id": draft.approval_id,
        "customer": draft.supplier_name, "est_amount": draft.est_amount,
        "note": ("quote draft recorded — a human reviews, sends and "
                 "turns it into an order"),
    }


QUOTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_quote_draft",
        "description": (
            "Create an approval-gated customer QUOTE draft (sales "
            "write-back). Only callable through an APPROVED ontology "
            "action — direct LLM calls are refused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "product": {"type": "string"},
                "quantity": {"type": "number"},
                "unit": {"type": "string"},
                "est_unit_price": {"type": "number"},
                "est_amount": {"type": "number"},
                "currency": {"type": "string"},
                "justification": {"type": "string"},
            },
            "required": ["customer_name"],
        },
    },
}


async def _quote_direct_call_guard(args, db: Session, user_id, context=None):
    ctx = context or {}
    if not ctx.get("approval_id"):
        return {
            "success": False,
            "reason": ("creating a quote draft is a WRITE action — it "
                       "requires a governance approval. Route it through "
                       "the ontology action 'quote_draft' (execute → admin "
                       "approves → execute-approved)."),
        }
    return await _create_quote_draft(args, db, user_id, context)


registry.register(
    name="create_quote_draft",
    schema=QUOTE_SCHEMA,
    handler=_quote_direct_call_guard,
    category="data",
    toolset="data",
    description="Approval-gated customer quote draft (sales write-back).",
    emoji="🧾",
    max_result_size_chars=20_000,
)


# ── P3-9: invoice matching ───────────────────────────────────────────────

async def _match_invoice(args: dict, db: Session, user_id: str | None,
                         context: dict | None = None) -> dict:
    from app.services.llm_service import chat_completion_json_sync
    from app.services.ontology.data import read_object_data
    from app.services.ontology.queries import get_object_by_name

    org_id = (context or {}).get("org_id") or "default-org"
    invoice_object = str(args.get("invoice_object") or "").strip()
    match_object = str(args.get("match_object") or "").strip()
    invoice_filters = args.get("invoice_filters") or {}
    if not invoice_object or not match_object:
        return {"success": False,
                "reason": "invoice_object and match_object are required"}

    inv_obj = get_object_by_name(db, org_id, invoice_object)
    mat_obj = get_object_by_name(db, org_id, match_object)
    if not inv_obj or not mat_obj:
        return {"success": False, "reason": "unknown ontology object"}
    for o, label in ((inv_obj, "invoice"), (mat_obj, "match")):
        if not (o.backing_ref and o.backing_kind != "manual"):
            return {"success": False,
                    "reason": f"{label} object {o.name!r} is not bound to live data"}

    inv = read_object_data(db, org_id, inv_obj, filters=invoice_filters, limit=10)
    if not inv.get("success") or not (inv.get("rows") or []):
        return {"success": False,
                "reason": f"no invoice rows found: {inv.get('reason') or 'empty'}"}
    mat = read_object_data(db, org_id, mat_obj, limit=50)
    if not mat.get("success") or not (mat.get("rows") or []):
        return {"success": False,
                "reason": f"no {match_object} rows found: {mat.get('reason') or 'empty'}"}

    prompt = (
        "You are doing invoice matching (2/3-way match). For EACH invoice "
        "row, find the best-matching record from the second set by the "
        "business meaning of the fields (supplier/counterparty, product/"
        "material, quantity, amount, dates). Judge ONLY from the fields "
        "given — if a needed field is missing, say so in the reason and "
        "lower your confidence.\n\n"
        f"INVOICE rows ({invoice_object}):\n"
        + __import__("json").dumps(inv["rows"], ensure_ascii=False, default=str)[:5000]
        + f"\n\nMATCH candidates ({match_object}):\n"
        + __import__("json").dumps(mat["rows"], ensure_ascii=False, default=str)[:5000]
        + "\n\nAnswer JSON: {\"matches\": [{\"invoice_index\": int, "
          "\"match_index\": int|null, \"verdict\": \"matched|discrepancy|unmatched\", "
          "\"confidence\": 0-1, \"reason\": \"cite the compared fields and any gap\"}]}"
    )
    schema = {
        "type": "object",
        "properties": {"matches": {"type": "array", "items": {"type": "object"}}},
        "required": ["matches"],
    }
    try:
        verdict = chat_completion_json_sync(prompt, schema=schema, temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"matching LLM failed: {exc}"}
    matches = (verdict or {}).get("matches") or []
    summary: dict[str, int] = {}
    for m in matches:
        v = str(m.get("verdict") or "unmatched")
        summary[v] = summary.get(v, 0) + 1
    return {
        "success": True,
        "invoice_object": invoice_object, "match_object": match_object,
        "matches": matches[:20], "summary": summary,
        "note": ("LLM-judged from live rows — field gaps lower confidence; "
                 "a human approves any discrepancy"),
    }


MATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "match_invoice",
        "description": (
            "Match invoices against POs/contracts/receipts (2/3-way "
            "match) from live bound data. Returns per-invoice verdicts "
            "(matched | discrepancy | unmatched) with cited evidence. "
            "Discrepancies go to a human — never auto-approved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "invoice_object": {"type": "string",
                                   "description": "Ontology object holding invoices."},
                "match_object": {"type": "string",
                                 "description": "Ontology object to match against (e.g. PurchaseOrder)."},
                "invoice_filters": {"type": "object",
                                    "description": "Optional filters to select the invoice rows."},
            },
            "required": ["invoice_object", "match_object"],
        },
    },
}

registry.register(
    name="match_invoice",
    schema=MATCH_SCHEMA,
    handler=_match_invoice,
    category="data",
    toolset="data",
    description="Invoice matching vs POs/contracts from live data (verdicts + evidence).",
    emoji="🔍",
    max_result_size_chars=30_000,
)
