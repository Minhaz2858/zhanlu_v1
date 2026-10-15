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
        "rows": _map_rows_to_properties(data, obj),
    }


def _map_rows_to_properties(rows: list[dict], obj: OntologyObject) -> list[dict]:
    """Rename column-keyed rows to the object's PROPERTY names — the
    ontology contract speaks business language, not physical columns."""
    mapping = {
        (p.get("column") or p.get("name")): p.get("name")
        for p in (obj.properties_json or [])
        if isinstance(p, dict)
    }
    if not mapping:
        return rows
    return [
        {mapping.get(k, k): v for k, v in row.items()} for row in rows
    ]


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
        "rows": _map_rows_to_properties(rows, obj),
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
    merged = {**(action.params_json or {}), **(params or {})}
    if action.risk_level == "write" or action.requires_confirmation:
        # P0-3: writes go through the governance approval flow — a pending
        # ApprovalRequest is created; an org admin approves via the existing
        # governance API; the caller then re-runs through
        # execute_approved_action with the approval id. Never auto-fire.
        from app.services.governance.approval_service import ApprovalService

        approval = ApprovalService(db).create_request(
            action_type="ontology_action",
            action_description=(
                f"Ontology action {action.name!r} "
                f"(tool={action.tool_name}) — {action.description or 'write-back'}"
            ),
            risk_tier="high",
            context_json={
                "action_id": action.id,
                "action_name": action.name,
                "tool_name": action.tool_name,
                "object_id": action.object_id,
                "params": merged,
            },
            org_id=org_id,
        )
        return {
            "success": False,
            "blocked": "confirmation_required",
            "approval_id": approval.id,
            "reason": (
                f"action {action.name!r} is risk_level={action.risk_level} — "
                "an approval request was created; an org admin must approve "
                "it (POST /api/approvals/{id}/approve), then call "
                "execute-approved"
            ),
        }

    import asyncio

    from app.services.agent_tools import execute_tool

    try:
        result = asyncio.run(
            execute_tool(
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


def execute_approved_action(
    db: Session,
    org_id: str,
    user_id: str | None,
    action_id: str,
    approval_id: str,
    params: dict | None = None,
    context: dict | None = None,
) -> dict:
    """Run a write-type ontology action after governance approval.

    The approval MUST be: found, owned by this org, status=approved, not
    expired, and bound to THIS action (context_json.action_id) with the
    same tool — an approval is a proof for one specific write, not a
    blanket permission (mirrors the agent-loop approval semantics).
    """
    from app.models.governance import ApprovalRequest
    from app.services.agent_tools import execute_tool

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
        return {"success": False, "reason": "action has no bound tool"}

    approval = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.org_id == org_id,
        )
        .first()
    )
    if not approval:
        return {"success": False, "reason": "approval not found in this org"}
    if approval.status != "approved":
        return {
            "success": False,
            "blocked": "confirmation_required",
            "reason": f"approval is {approval.status!r}, not approved",
        }
    ctx = approval.context_json or {}
    if ctx.get("action_id") != action_id or ctx.get("tool_name") != action.tool_name:
        return {
            "success": False,
            "reason": "approval does not match this action (id/tool mismatch)",
        }
    if approval.expires_at is not None:
        from datetime import datetime, timezone

        expires = approval.expires_at
        if getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return {"success": False, "reason": "approval has expired"}

    merged = {**(ctx.get("params") or {}), **(action.params_json or {}),
              **(params or {})}
    import asyncio

    try:
        result = asyncio.run(
            execute_tool(
                action.tool_name,
                merged,
                db,
                user_id,
                context={
                    "org_id": org_id,
                    "ontology_action_id": action.id,
                    "ontology_object_id": action.object_id,
                    "approval_id": approval.id,
                    **(context or {}),
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 — report, don't raise
        logger.warning(
            "approved action %s tool %s failed: %s",
            action.name, action.tool_name, exc,
        )
        return {"success": False,
                "reason": f"tool {action.tool_name} failed: {exc}"}

    return {
        "success": bool(result.get("success")),
        "action": action.name,
        "tool": action.tool_name,
        "approval_id": approval.id,
        "approved_by": approval.reviewed_by,
        "result": result,
    }
