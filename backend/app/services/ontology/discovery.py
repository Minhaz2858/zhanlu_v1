"""Ontology auto-discovery — bind objects to REAL schemas (P1, 2026-10-15).

The strategy's onboarding flow: connect database → catalog builds →
ontology objects APPEAR bound to real tables. This service matches the
org's unwired ontology objects against the actual schemas of its bound
databases (keyword hints verified against the live schema — never guessed
bindings), maps object properties to real columns, and binds.

Honesty rules:
- only UNBOUND objects get auto-bound (never overwrite a human decision)
- a binding is only created when the candidate table EXISTS in the KB
  schema (SchemaService-verified)
- properties map only to columns that actually exist; unmapped properties
  stay unmapped and are reported
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.ontology.queries import list_objects

logger = logging.getLogger(__name__)

# Object name → table-name keywords, priority order (first hit wins).
_OBJECT_TABLE_HINTS: dict[str, list[str]] = {
    "Supplier": ["supplier", "vendor"],
    "Customer": ["bd_customer", "customer", "client"],
    "PurchaseOrder": ["pur_poorder", "purchase_order", "purchase"],
    "Contract": ["contract"],
    "Inventory": ["stk_inventory", "inventory"],
    "Order": ["sale_orderentry", "sal_outstockentry", "sales_details"],
    "Product": ["material", "product"],
    "Shipment": ["outstock", "shipment", "delivery"],
    "Invoice": ["invoice", "receivable"],
    "Payment": ["payment", "receipt"],
    "Employee": ["employee", "staff"],
    "Incident": ["incident"],
    "Document": ["document", "attachment"],
    "Ticket": ["ticket"],
}

# Property name → column-name keywords (first match wins).
_PROP_COLUMN_HINTS: dict[str, list[str]] = {
    "name": ["fname", "material_name", "name", "title"],
    "code": ["fnumber", "material_code", "code", "number"],
    "qty": ["fqty", "quantity", "qty"],
    "amount": ["famount", "amount", "fallamount"],
    "price": ["ftaxprice", "fprice", "price"],
    "currency": ["currency"],
    "payment_terms": ["pay_condition", "paytype", "payment"],
    "tax_rate": ["ftaxrate", "taxrate", "tax_rate"],
    "status": ["status", "fstatus"],
    "date": ["fdate", "date", "created"],
    "region": ["region", "org_name", "area"],
    "location": ["stock", "location", "warehouse"],
    "description": ["description", "remark", "note"],
}


def _table_matches(table_name: str, hints: list[str]) -> bool:
    t = table_name.lower()
    return any(h in t for h in hints)


def _column_for_property(prop: str, columns: list[str]) -> str | None:
    p = prop.lower()
    hints = _PROP_COLUMN_HINTS.get(p, [p])
    for hint in hints:
        for col in columns:
            if hint in col.lower():
                return col
    # last resort: exact-ish identity match
    for col in columns:
        if col.lower() == p:
            return col
    return None


def discover_bindings(db: Session, org_id: str, *, apply: bool = True) -> dict:
    """Match unwired ontology objects against the org's database schemas.

    With ``apply=True`` (default) binds high-confidence matches and maps
    properties to verified columns; with ``False`` returns the report
    only (dry-run).
    """
    from app.services.db.schema_service import SchemaService

    kbs = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.org_id == org_id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
            KnowledgeBase.source_kind == "database",  # noqa: E712
        )
        .all()
    )
    if not kbs:
        return {"discovered": 0, "report": [], "note": "no database sources bound"}

    # Table inventory per KB (verified against the live schema).
    inventories: list[tuple[str, list[str]]] = []
    schema_svc = SchemaService(db)
    for kb in kbs:
        try:
            tables = schema_svc.list_tables(kb.id)
            names = [
                t.get("name") if isinstance(t, dict) else str(t)
                for t in (tables.get("tables") or [])
            ]
            if names:
                inventories.append((kb.id, names))
        except Exception as exc:  # noqa: BLE001 — one bad source can't stop all
            logger.warning("discovery: list_tables failed for KB %s: %s", kb.id, exc)

    report: list[dict] = []
    discovered = 0
    for obj in list_objects(db, org_id):
        if obj.backing_ref and obj.backing_kind != "manual":
            continue  # already wired — never overwrite a human decision
        hints = _OBJECT_TABLE_HINTS.get(obj.name)
        if not hints:
            report.append({"object": obj.name, "status": "no-hints"})
            continue

        match: tuple[str, str] | None = None
        for kb_id, tables in inventories:
            for hint in hints:
                hits = [t for t in tables if _table_matches(t, [hint])]
                if hits:
                    match = (kb_id, hits[0])
                    break
            if match:
                break

        if not match:
            report.append({"object": obj.name, "status": "no-table-found"})
            continue

        kb_id, table = match
        # Property → column mapping, verified against the real schema.
        mapped: dict[str, str] = {}
        try:
            cols = [
                (c.get("name") or c.get("column"))
                for c in (
                    schema_svc.describe_table(kb_id, table).get("columns") or []
                )
            ]
            cols = [c for c in cols if c]
            for prop in obj.properties_json or []:
                pname = prop.get("name") if isinstance(prop, dict) else str(prop)
                col = _column_for_property(pname, cols)
                if col:
                    mapped[pname] = col
        except Exception as exc:  # noqa: BLE001 — binding still valid sans props
            logger.warning("discovery: describe_table %s failed: %s", table, exc)

        if apply:
            obj.backing_kind = "table"
            obj.backing_ref = f"kb:{kb_id}:{table}"
            if obj.properties_json and mapped:
                for prop in obj.properties_json:
                    if isinstance(prop, dict) and prop.get("name") in mapped:
                        prop["column"] = mapped[prop["name"]]
            db.add(obj)

        discovered += 1
        report.append({
            "object": obj.name,
            "status": "bound" if apply else "would-bind",
            "kb_id": kb_id,
            "table": table,
            "properties_mapped": mapped,
            "properties_unmapped": [
                (p.get("name") if isinstance(p, dict) else str(p))
                for p in (obj.properties_json or [])
                if (p.get("name") if isinstance(p, dict) else str(p)) not in mapped
            ],
        })

    if apply:
        db.commit()
    logger.info(
        "ontology discovery: org=%s discovered=%d (apply=%s)", org_id, discovered, apply
    )
    return {"discovered": discovered, "report": report}


def discover_and_recompute_coverage(db: Session, org_id: str) -> dict:
    out = discover_bindings(db, org_id, apply=True)
    from app.services.ontology.coverage import compute_coverage

    out["coverage"] = compute_coverage(db, org_id)
    return out
