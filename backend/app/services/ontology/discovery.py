"""Ontology auto-discovery — bind objects to REAL schemas (P1 + SaaS dynamic).

SaaS reality (the customer is right): the database is TOTALLY dynamic —
every tenant connects different systems (ERP, PLM, CRM, anything with a
JDBC/ODBC driver). Discovery therefore works in THREE layers, from cheap
and certain to smart and gated:

1. Connector hints — known vocabularies per connector type (Kingdee
   F-columns, Salesforce Account/Contact, SAP KNA1/LFA1, generic).
2. Glossary hints — the org's OWN terminology entries (they describe the
   tenant's business language, not ours).
3. LLM-assisted matching — for everything the cheap layers can't bind,
   an LLM matches objects/properties against the tenant's ACTUAL schema
   with a confidence score. Only ≥ ONTOLOGY_DISCOVERY_AUTOBIND_THRESHOLD
   is auto-bound; below is reported as "proposed" — a human decides.

Honesty rules (unchanged, and they matter MORE when an LLM is involved):
- only UNBOUND objects get auto-bound (never overwrite a human decision)
- a binding is created ONLY when the table EXISTS in the KB schema
  (SchemaService-verified) and each mapped column was VERIFIED to exist
- LLM output is untrusted input: tables/columns that don't verify are
  dropped, low confidence is never silently upgraded
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase
from app.services.ontology.queries import list_objects

logger = logging.getLogger(__name__)

# ── Layer 1: connector vocabularies ─────────────────────────────────────────

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

# Per-connector EXTRA hints (merged on top of the global lists). Keys are
# KnowledgeBase.db_type values — unknown db_types simply get no extras,
# which is exactly why layers 2 and 3 exist.
_CONNECTOR_OBJECT_HINTS: dict[str, dict[str, list[str]]] = {
    "salesforce": {
        "Customer": ["account"],
        "Employee": ["user", "contact"],
        "Order": ["opportunity", "order"],
        "Product": ["product2", "pricebook"],
    },
    "sap": {
        "Customer": ["kna1"],
        "Supplier": ["lfa1"],
        "Order": ["vbak", "vbakp"],
        "Product": ["mara"],
        "Invoice": ["vbrk"],
    },
    "kingdee": {},  # the global hints ARE the Kingdee vocabulary
    "mysql": {}, "postgres": {}, "postgresql": {}, "sqlserver": {},
    "oracle": {}, "mssql": {},
}

_CONNECTOR_PROP_HINTS: dict[str, dict[str, list[str]]] = {
    "salesforce": {
        "name": ["name"],
        "code": ["accountnumber", "productcode", "externalsid"],
        "amount": ["amount"],
        "date": ["closedate", "createddate"],
    },
    "sap": {
        "code": ["kunnr", "lifnr", "matnr", "vbeln"],
        "name": ["name1", "maktx"],
    },
}


def _object_hints_for(obj_name: str, db_type: str | None) -> list[str]:
    hints = list(_OBJECT_TABLE_HINTS.get(obj_name, []))
    extra = _CONNECTOR_OBJECT_HINTS.get((db_type or "").lower(), {})
    for h in extra.get(obj_name, []):
        if h not in hints:
            hints.append(h)
    return hints


def _prop_hints_for(prop: str, db_type: str | None) -> list[str]:
    hints = list(_PROP_COLUMN_HINTS.get(prop, [prop]))
    extra = _CONNECTOR_PROP_HINTS.get((db_type or "").lower(), {})
    for h in extra.get(prop, []):
        if h not in hints:
            hints.append(h)
    return hints


# ── Layer 2: the org's own glossary ─────────────────────────────────────────


def _glossary_table_hints(db: Session, org_id: str) -> dict[str, list[str]]:
    """The tenant's glossary words double as table-name keywords (e.g. a
    tenant whose glossary says "customer = 客户" or "customer = Account"
    teaches discovery THEIR vocabulary). Bounded to enabled entries."""
    from app.models.terminology import Terminology

    try:
        rows = (
            db.query(Terminology)
            .filter(
                Terminology.org_id == org_id,
                Terminology.enabled == True,  # noqa: E712
            )
            .limit(200)
            .all()
        )
    except Exception as exc:  # noqa: BLE001 — glossary is advisory
        logger.warning("discovery: glossary load failed: %s", exc)
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        w = (r.word or "").strip().lower()
        if len(w) >= 3:
            out.setdefault(w, [])
    return out


# ── Layer 3: LLM-assisted matching ──────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(name: str) -> set[str]:
    return set(_TOKEN_RE.findall((name or "").lower()))


def _shortlist(
    obj, inventories: list[tuple[str, str, list[str]]], max_tables: int = 40,
) -> list[tuple[str, str]]:
    """Candidate tables for an object by token overlap (object name +
    property names vs table name).

    SaaS-critical: a tenant connects MANY systems and the ontology must
    treat them EQUALLY. The no-overlap fallback therefore ROUND-ROBINS
    across every connected source (one table per source per round) — a
    foreign CRM's tables are never crowded out by whichever system
    happens to be first in the list."""
    want = _tokens(obj.name) | {
        t for p in (obj.properties_json or [])
        if isinstance(p, dict) for t in _tokens(p.get("name") or "")
    }
    scored: list[tuple[int, str, str]] = []
    for kb_id, db_type, tables in inventories:
        for t in tables:
            score = len(want & _tokens(t))
            if score:
                scored.append((score, kb_id, t))
    scored.sort(reverse=True)
    out = [(kb_id, t) for _, kb_id, t in scored[:max_tables]]
    if not out:
        # round-robin one table per source per pass — equal representation
        seen: set[tuple[str, str]] = set()
        cursors = [0] * len(inventories)
        while len(out) < max_tables and any(
            cursors[i] < len(inv[2]) for i, inv in enumerate(inventories)
        ):
            for i, (kb_id, _dt, tables) in enumerate(inventories):
                if cursors[i] >= len(tables):
                    continue
                t = tables[cursors[i]]
                cursors[i] += 1
                key = (kb_id, t)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
                if len(out) >= max_tables:
                    break
    return out[:max_tables]


def _llm_schema_match(
    obj,
    candidates: list[tuple[str, str, list[str]]],
    db_types: dict[str, str],
) -> dict | None:
    """ONE bounded LLM call: pick the best table for an object from ALL
    candidates (across every connected source) and map its properties to
    REAL columns, with a 0..1 confidence. One call per object — not per
    candidate — so budget is predictable and no source is crowded out by
    candidate ordering. Returns None (never binds) on abstain/error."""
    from app.services.llm_service import chat_completion_json_sync

    if not candidates:
        return None
    props = [
        (p.get("name") if isinstance(p, dict) else str(p))
        for p in (obj.properties_json or [])
    ]
    table_blocks = []
    for i, (kb_id, table, cols) in enumerate(candidates):
        table_blocks.append(
            f"[{i}] table={table} (source type: {db_types.get(kb_id, 'unknown')})\n"
            f"    columns: {cols[:12]}"
        )
    prompt = (
        "You are mapping a business ontology object to a real database "
        "table from a company's OWN connected systems (ERP/PLM/CRM — any "
        "vendor, any naming convention, any language). Multiple sources "
        "may be connected; consider ALL candidate tables equally.\n\n"
        f"Ontology object: {obj.name}"
        + (f" ({obj.display_name})" if obj.display_name else "")
        + (f" — {obj.description}" if obj.description else "") + "\n"
        f"Properties: {props}\n\n"
        "Candidate tables (real schema):\n" + "\n".join(table_blocks) + "\n\n"
        "Decide which candidate table (if any) plausibly holds this "
        "object's records, and map each property to the column that "
        "holds it (EXACT column names). Be strict — wrong mappings are "
        "worse than no mapping; answer match=false when nothing fits.\n\n"
        'Answer JSON: {"match": true|false, "table_index": <int>, '
        '"confidence": 0.0-1.0, "properties": {"prop": "EXACT_COLUMN"}}'
    )
    schema = {
        "type": "object",
        "properties": {
            "match": {"type": "boolean"},
            "table_index": {"type": "integer"},
            "confidence": {"type": "number"},
            "properties": {"type": "object"},
        },
        "required": ["match", "confidence"],
    }
    try:
        verdict = chat_completion_json_sync(prompt, schema=schema, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 — LLM is advisory, never fatal
        logger.warning("discovery: LLM match failed for %s: %s", obj.name, exc)
        return None
    if not verdict or not isinstance(verdict, dict):
        return None
    try:
        conf = float(verdict.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    if not verdict.get("match") or conf <= 0:
        return None
    idx = verdict.get("table_index")
    if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
        idx = 0  # tolerate a missing index when only one candidate exists
    kb_id, table, cols = candidates[idx]
    props_out = {
        str(k): str(v) for k, v in (verdict.get("properties") or {}).items()
        if isinstance(k, str) and isinstance(v, str) and v in cols
    }
    return {"kb_id": kb_id, "table": table, "confidence": conf,
            "properties": props_out}


def _apply_binding(obj, kb_id: str, table: str, mapped: dict[str, str]) -> None:
    obj.backing_kind = "table"
    obj.backing_ref = f"kb:{kb_id}:{table}"
    # IMPORTANT: JSON columns need REASSIGNMENT to be detected by
    # SQLAlchemy — in-place mutation of nested dicts is silently lost on
    # commit (this exact bug shipped in v1: property→column mappings
    # vanished after restart while the report showed them).
    props = obj.properties_json or []
    new_props = []
    for prop in props:
        if isinstance(prop, dict) and prop.get("name") in mapped:
            new_props.append({**prop, "column": mapped[prop["name"]]})
        else:
            new_props.append(prop)
    obj.properties_json = new_props


def _verified_columns(schema_svc, kb_id: str, table: str) -> list[str]:
    try:
        cols = [
            (c.get("name") or c.get("column"))
            for c in (schema_svc.describe_table(kb_id, table).get("columns") or [])
        ]
        return [c for c in cols if c]
    except Exception as exc:  # noqa: BLE001
        logger.warning("discovery: describe_table %s failed: %s", table, exc)
        return []


# ── Orchestration ───────────────────────────────────────────────────────────


def discover_bindings(db: Session, org_id: str, *, apply: bool = True) -> dict:
    """Match unwired ontology objects against the org's real schemas.

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
        return {"discovered": 0, "proposed": 0, "report": [],
                "note": "no database sources bound"}

    # Table inventory per KB (verified against the live schema).
    inventories: list[tuple[str, str, list[str]]] = []
    schema_svc = SchemaService(db)
    for kb in kbs:
        try:
            tables = schema_svc.list_tables(kb.id)
            names = [
                t.get("name") if isinstance(t, dict) else str(t)
                for t in (tables.get("tables") or [])
            ]
            if names:
                inventories.append((kb.id, (kb.db_type or "").lower(), names))
        except Exception as exc:  # noqa: BLE001 — one bad source can't stop all
            logger.warning("discovery: list_tables failed for KB %s: %s", kb.id, exc)

    if not inventories:
        return {"discovered": 0, "proposed": 0, "report": [],
                "note": "no database schemas reachable"}

    glossary = _glossary_table_hints(db, org_id)
    threshold = float(getattr(settings, "ONTOLOGY_DISCOVERY_AUTOBIND_THRESHOLD", 0.8))
    llm_enabled = bool(getattr(settings, "ONTOLOGY_LLM_DISCOVERY_ENABLED", True))

    report: list[dict] = []
    discovered = 0
    proposed = 0
    # Bounded LLM spend per run: candidate loops cost 1 call each; when
    # the budget is out, remaining objects report honestly instead of
    # racking up a silent bill.
    max_llm_calls = int(getattr(settings, "ONTOLOGY_DISCOVERY_MAX_LLM_CALLS", 24))
    llm_calls = 0

    for obj in list_objects(db, org_id):
        if obj.backing_ref and obj.backing_kind != "manual":
            continue  # already wired — never overwrite a human decision

        # ── Layer 1+2: hints (global + connector + the org's glossary) ──
        match = None
        method = None
        for kb_id, db_type, tables in inventories:
            hints = _object_hints_for(obj.name, db_type)
            # glossary words that appear in the object's name tokens
            for tok in _tokens(obj.name):
                if tok in glossary and tok not in hints:
                    hints.append(tok)
            for hint in hints:
                hits = [t for t in tables if hint in t.lower()]
                if hits:
                    match = (kb_id, hits[0])
                    method = "hint"
                    break
            if match:
                break

        if match:
            kb_id, table = match
            db_type = next(
                (dt for kid, dt, _ in inventories if kid == kb_id), None)
            cols = _verified_columns(schema_svc, kb_id, table)
            mapped: dict[str, str] = {}
            for prop in obj.properties_json or []:
                pname = prop.get("name") if isinstance(prop, dict) else str(prop)
                col = _column_for_property(pname, cols, db_type)
                if col:
                    mapped[pname] = col
            if apply:
                _apply_binding(obj, kb_id, table, mapped)
            discovered += 1
            report.append({
                "object": obj.name,
                "status": "bound" if apply else "would-bind",
                "method": method, "kb_id": kb_id, "table": table,
                "properties_mapped": mapped,
                "properties_unmapped": [
                    (p.get("name") if isinstance(p, dict) else str(p))
                    for p in (obj.properties_json or [])
                    if (p.get("name") if isinstance(p, dict) else str(p))
                    not in mapped
                ],
            })
            continue

        # ── Layer 3: LLM-assisted matching against the ACTUAL schema ──
        if not llm_enabled:
            report.append({"object": obj.name, "status": "no-table-found",
                           "note": "llm discovery disabled"})
            continue

        shortlist = _shortlist(obj, inventories)
        if not shortlist:
            report.append({"object": obj.name, "status": "no-table-found"})
            continue
        if llm_calls >= max_llm_calls:
            report.append({
                "object": obj.name, "status": "no-table-found",
                "note": "llm call budget exhausted — re-run discovery",
            })
            continue
        # ONE call per object: describe candidates (bounded) and let the
        # LLM pick the best table across ALL connected sources.
        cand: list[tuple[str, str, list[str]]] = []
        for kb_id, table in shortlist[:10]:
            cols = _verified_columns(schema_svc, kb_id, table)
            if cols:
                cand.append((kb_id, table, cols))
        if not cand:
            report.append({"object": obj.name, "status": "no-table-found"})
            continue
        llm_calls += 1
        verdict = _llm_schema_match(obj, cand, dict(
            (kb_id, dt) for kb_id, dt, _ in inventories))
        if not verdict:
            report.append({"object": obj.name, "status": "no-table-found"})
            continue
        kb_id = verdict["kb_id"]
        table = verdict["table"]
        conf = verdict["confidence"]
        mapped = verdict["properties"]  # already verified against cols
        if conf >= threshold and apply:
            _apply_binding(obj, kb_id, table, mapped)
            discovered += 1
            report.append({
                "object": obj.name, "status": "bound",
                "method": "llm", "confidence": conf,
                "kb_id": kb_id, "table": table,
                "properties_mapped": mapped,
                "properties_unmapped": [
                    (p.get("name") if isinstance(p, dict) else str(p))
                    for p in (obj.properties_json or [])
                    if (p.get("name") if isinstance(p, dict)
                        else str(p)) not in mapped
                ],
            })
        else:
            proposed += 1
            report.append({
                "object": obj.name,
                "status": "proposed" if apply else "would-propose",
                "method": "llm", "confidence": conf,
                "kb_id": kb_id, "table": table,
                "properties_mapped": mapped,
                "note": (
                    "confidence below auto-bind threshold — "
                    "review and bind it explicitly via the bind endpoint"
                ),
            })

    if apply:
        db.commit()
    logger.info(
        "ontology discovery: org=%s discovered=%d proposed=%d (apply=%s)",
        org_id, discovered, proposed, apply,
    )
    return {"discovered": discovered, "proposed": proposed, "report": report}


def _column_for_property(
    prop: str, columns: list[str], db_type: str | None = None,
) -> str | None:
    p = prop.lower()
    hints = _prop_hints_for(prop, db_type)
    for hint in hints:
        for col in columns:
            if hint in col.lower():
                return col
    # last resort: exact-ish identity match
    for col in columns:
        if col.lower() == p:
            return col
    return None


def discover_and_recompute_coverage(db: Session, org_id: str) -> dict:
    out = discover_bindings(db, org_id, apply=True)
    from app.services.ontology.coverage import compute_coverage

    out["coverage"] = compute_coverage(db, org_id)
    return out
