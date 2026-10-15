"""Ontology grounded reads + risk-tiered action execution (P0-2)."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.models.ontology import OntologyAction, OntologyObject

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_object_data(
    db: Session,
    org_id: str,
    obj: OntologyObject,
    filters: dict | None = None,
    limit: int = 50,
) -> dict:
    """Grounded read of an object's live data.

    Two backing shapes (backing_kind=table):
      • ``<table>``           → a table in the PLATFORM database (the
        request's pinned session; RLS still applies).
      • ``kb:<kb_id>:<table>``→ a table in the TENANT's own database
        (BYO-DB) — routed through the read-only QueryService after an
        org-ownership check on the KB.

    entity/metric backings return an honest "not yet supported" (P1).
    """
    try:
        limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    filters = {k: v for k, v in (filters or {}).items() if v is not None}

    if obj.backing_kind != "table" or not obj.backing_ref:
        return {
            "success": False,
            "reason": (
                f"object {obj.name!r} has no live table binding "
                f"(backing_kind={obj.backing_kind}) — bind it to a data "
                "source to read live rows"
            ),
        }

    ref = obj.backing_ref
    if ref.startswith("kb:"):
        parts = ref.split(":", 2)
        if len(parts) != 3 or not _IDENT_RE.match(parts[2]):
            return {"success": False, "reason": f"invalid backing ref {ref!r}"}
        return _read_via_kb(db, org_id, obj, parts[1], parts[2], filters, limit)

    if not _IDENT_RE.match(ref):
        return {"success": False, "reason": f"invalid backing table {ref!r}"}

    props = obj.properties_json or []
    select_cols = [
        (p.get("column") or p.get("name")) for p in props if isinstance(p, dict)
    ]
    select_cols = [c for c in select_cols if c and _IDENT_RE.match(c)] or ["*"]

    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    for key, value in filters.items():
        if _IDENT_RE.match(str(key)):
            params[f"f_{key}"] = value
            where_clauses.append(f"{key} = :f_{key}")

    sql = f"SELECT {', '.join(select_cols)} FROM {ref}"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += f" LIMIT {limit}"

    try:
        rows = db.execute(sa_text(sql), params).mappings().all()
        data = [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001 — honest failure
        logger.warning("ontology read_object_data failed (%s): %s", obj.name, exc)
        return {
            "success": False,
            "reason": f"backing query failed: {exc}",
            "hint": "never invent data — report the failure",
        }

    return {
        "success": True,
        "object": obj.name,
        "domain_id": obj.domain_id,
        "backing": {"kind": "table", "ref": ref},
        "row_count": len(data),
        "rows": data,
    }


def _read_via_kb(
    db: Session,
    org_id: str,
    obj: OntologyObject,
    kb_id: str,
    table: str,
    filters: dict,
    limit: int,
) -> dict:
    """Route a tenant-database read through the read-only QueryService,
    after verifying the KB belongs to THIS org (cross-tenant reads are a
    leak, not a feature)."""
    from app.models.knowledge_base import KnowledgeBase

    kb = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not kb or (kb.org_id or "default-org") != org_id:
        return {"success": False, "reason": "data source not found in this org"}

    props = obj.properties_json or []
    select_cols = [
        (p.get("column") or p.get("name")) for p in props if isinstance(p, dict)
    ]
    select_cols = [c for c in select_cols if c and _IDENT_RE.match(c)] or ["*"]

    where_clauses = [
        f"{key} = :f_{key}"
        for key in filters
        if _IDENT_RE.match(str(key))
    ]
    params = {f"f_{k}": v for k, v in filters.items()
              if _IDENT_RE.match(str(k))}

    sql = f"SELECT {', '.join(select_cols)} FROM {table}"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    # No inline LIMIT: MSSQL/Oracle reject it — QueryService caps rows and
    # PROVES truncation (max_rows + truncated flag).

    try:
        from app.services.db.query_service import QueryService

        result = QueryService(db).execute(kb_id, sql, max_rows=limit,
                                          params=params or None)
        rows = result.get("rows") or []
    except Exception as exc:  # noqa: BLE001 — honest failure
        logger.warning("ontology kb read failed (%s): %s", obj.name, exc)
        return {"success": False, "reason": f"backing query failed: {exc}",
                "hint": "never invent data — report the failure"}

    return {
        "success": True,
        "object": obj.name,
        "domain_id": obj.domain_id,
        "backing": {"kind": "kb", "kb_id": kb_id, "ref": table},
        "row_count": len(rows),
        "rows": rows,
        "truncated": bool(result.get("truncated")),
    }


def execute_action(
    db: Session,
    org_id: str,
    user_id: str | None,
    action_id: str,
    params: dict | None = None,
    context: dict | None = None,
) -> dict:
    """Execute an ontology action under the risk contract.

    read → run the bound registry tool now. write → human confirmation
    REQUIRED (governance approval wiring, P0-3); never auto-fires.
    """
    action = (
        db.query(OntologyAction)
        .filter(
            OntologyAction.id == action_id,
            OntologyAction.org_id == org_id,
            OntologyAction.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not action:
        return {"success": False, "reason": "action not found"}
    if not action.tool_name:
        return {
            "success": False,
            "reason": (
                f"action {action.name!r} has no bound tool — it is a "
                "proposal until wired to a registry tool"
            ),
        }
    if action.risk_level == "write" or action.requires_confirmation:
        return {
            "success": False,
            "blocked": "confirmation_required",
            "reason": (
                f"action {action.name!r} is risk_level={action.risk_level} — "
                "write-backs require human confirmation (governance approval)"
            ),
        }

    from app.services.agent_tools import execute_tool

    merged = {**(action.params_json or {}), **(params or {})}
    try:
        result = execute_tool(
            action.tool_name,
            merged,
            db,
            user_id,
            context={
                "org_id": org_id,
                "ontology_action_id": action.id,
                "ontology_object_id": action.object_id,
                **(context or {}),
            },
        )
    except Exception as exc:  # noqa: BLE001 — report, don't raise
        logger.warning(
            "ontology action %s tool %s failed: %s",
            action.name, action.tool_name, exc,
        )
        return {
            "success": False,
            "reason": f"tool {action.tool_name} failed: {exc}",
            "hint": "tell the user the action could not be completed",
        }

    return {
        "success": bool(result.get("success")),
        "action": action.name,
        "tool": action.tool_name,
        "risk_level": action.risk_level,
        "result": result,
    }
