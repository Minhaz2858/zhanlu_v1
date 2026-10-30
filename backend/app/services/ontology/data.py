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

# P2-3 multimodal foundation: property types whose values may be media
# references. A URL-valued cell of these types is surfaced as an
# attachment ({media_url, media_type}) instead of a bare string.
_MEDIA_TYPES = {"image", "file", "audio", "video"}


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

    entity/metric backings: metric → governed computation via
    services/ontology/metrics.compute_metric (P5-T2); entity → confirmed
    crosswalk resolution (P5-T4). Anything unresolvable returns a TYPED
    unavailable result — never a fabricated number.
    """
    try:
        limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    filters = {k: v for k, v in (filters or {}).items() if v is not None}

    if obj.backing_kind == "metric" and (obj.backing_ref or "").strip():
        from app.services.ontology.metrics import compute_metric

        out = compute_metric(db, org_id, obj.backing_ref, filters=filters)
        if out.get("success"):
            out["object"] = obj.name
        return out

    if obj.backing_kind == "entity" and (obj.backing_ref or "").strip():
        return _resolve_entity_backing(db, org_id, obj, filters, limit)

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
        **_cross_sources_block(db, org_id, obj),
    }


def _map_rows_to_properties(rows: list[dict], obj: OntologyObject) -> list[dict]:
    """Rename column-keyed rows to the object's PROPERTY names — the
    ontology contract speaks business language, not physical columns.

    P2-3 multimodal foundation: properties typed as image/file/audio/
    video whose value looks like a URL are wrapped as media attachments
    (modality-agnostic contract — values stay honest, never fabricated)."""
    mapping = {
        (p.get("column") or p.get("name")): p
        for p in (obj.properties_json or [])
        if isinstance(p, dict)
    }
    if not mapping:
        return rows

    def _wrap(value, prop: dict):
        if value is None:
            return None
        ptype = (prop.get("type") or "").lower()
        if ptype in _MEDIA_TYPES and isinstance(value, str):
            v = value.strip()
            if v.startswith(("http://", "https://", "/uploads/", "/files/")):
                return {"media_url": v, "media_type": ptype}
        return value

    return [
        {
            (mapping.get(k, {}).get("name") if k in mapping else k):
                _wrap(v, mapping.get(k, {})) if k in mapping else v
            for k, v in row.items()
        }
        for row in rows
    ]


def _resolve_entity_backing(
    db: Session,
    org_id: str,
    obj: OntologyObject,
    filters: dict,
    limit: int,
) -> dict:
    """P5-T4 — entity backing over CONFIRMED identity crosswalks only.

    For every confirmed source pair the SAME records are returned per
    source (each side keeps its OWN identifiers and rows — no destructive
    merge, invariant I5). Inferred/rejected matches are never promoted at
    runtime: no confirmed crosswalk → typed unavailable.
    """
    from app.models.identity import FileTableRow, IdentityMatch
    from app.models.knowledge_base import KnowledgeBase

    matches = (
        db.query(IdentityMatch)
        .filter(
            IdentityMatch.org_id == org_id,
            IdentityMatch.object_id == obj.id,
            IdentityMatch.status == "confirmed",
            IdentityMatch.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    if not matches:
        return _unavailable_shape(
            f"object {obj.name!r} has no CONFIRMED identity crosswalk — "
            "confirm one in the identity review queue to enable entity reads")

    groups: dict[tuple[str, str], list[IdentityMatch]] = {}
    for m in matches:
        groups.setdefault((m.source_a_key, m.source_b_key), []).append(m)

    def _rows_for(source: dict, pks: list[str]) -> dict:
        """Grounded rows for ONE side of a crosswalk. DB sources go through
        the org-checked QueryService; file sources read the P3a staging."""
        kind = (source or {}).get("kind")
        pk_col = (source or {}).get("pk_column") or (source or {}).get("column")
        kb_id = (source or {}).get("kb_id")
        try:
            if kind == "file":
                rows = (
                    db.query(FileTableRow)
                    .filter(
                        FileTableRow.kb_id == kb_id,
                        FileTableRow.sheet_name == (source or {}).get("sheet"),
                        FileTableRow.is_deleted == False,  # noqa: E712
                    )
                    .all()
                )
                col = pk_col or ""
                out_rows = [
                    r.values_json for r in rows
                    if str((r.values_json or {}).get(col)) in set(pks)
                ]
                return {"kind": "file", "kb_id": kb_id,
                        "sheet": (source or {}).get("sheet"),
                        "pk_column": col, "rows": out_rows[:limit]}
            kb = db.query(KnowledgeBase).filter(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.is_deleted == False,  # noqa: E712
            ).first()
            if kb is None or (kb.org_id or "default-org") != org_id:
                return {"kind": "db", "kb_id": kb_id, "rows": [],
                        "reason": "source not in this org"}
            if not (_IDENT_RE.match(str(pk_col or ""))
                    and _IDENT_RE.match(str((source or {}).get("table") or ""))):
                return {"kind": "db", "kb_id": kb_id, "rows": [],
                        "reason": "source pk/table not resolvable"}
            from app.services.db.query_service import QueryService

            placeholders = ", ".join(f":pk{i}" for i in range(len(pks)))
            sql = (f"SELECT * FROM {source['table']} "
                   f"WHERE {pk_col} IN ({placeholders})")
            result = QueryService(db).execute(
                kb_id, sql, max_rows=limit,
                params={f"pk{i}": v for i, v in enumerate(pks)})
            return {"kind": "db", "kb_id": kb_id,
                    "table": source.get("table"), "pk_column": pk_col,
                    "rows": result.get("rows") or []}
        except Exception as exc:  # noqa: BLE001 — honest per-source failure
            logger.warning("entity backing source read failed: %s", exc)
            return {"kind": kind, "kb_id": kb_id, "rows": [],
                    "reason": f"source read failed: {exc}"}

    out_groups = []
    for (key_a, key_b), ms in groups.items():
        first = ms[0]
        pks_a = [m.pk_a for m in ms]
        pks_b = [m.pk_b for m in ms]
        out_groups.append({
            "source_a_key": key_a,
            "source_b_key": key_b,
            "confirmed_pairs": len(ms),
            "side_a": _rows_for(first.source_a or {}, pks_a),
            "side_b": _rows_for(first.source_b or {}, pks_b),
        })

    return {
        "success": True,
        "object": obj.name,
        "backing_kind": "entity",
        "crosswalk_groups": out_groups,
        "note": "per-source records only — no destructive merge",
    }


def _unavailable_shape(reason: str) -> dict:
    return {"success": False, "unavailable": True,
            "evidence_status": "unavailable", "reason": reason}


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
    # Only properties with a VERIFIED column are selectable — unmapped
    # properties have no physical column (partial LLM mappings are
    # legitimate) and falling back to the property name would query a
    # column that does not exist.
    select_cols = [
        p.get("column") for p in props
        if isinstance(p, dict) and p.get("column")
    ]
    select_cols = [c for c in select_cols if c and _IDENT_RE.match(c)]
    if not select_cols:
        return {
            "success": False,
            "reason": (
                f"object {obj.name!r} has no property mapped to a real "
                "column yet — map properties via the bind endpoint"
            ),
            "hint": "never invent data — report the failure",
        }

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
        **_cross_sources_block(db, org_id, obj),
    }


def _cross_sources_block(db: Session, org_id: str, obj: OntologyObject) -> dict:
    """P3 runtime integration: objects with CONFIRMED identity crosswalks
    expose an honest `cross_sources` provenance summary. Inferred/rejected
    matches never appear; failure to compute degrades silently (never
    blocks the read)."""
    try:
        from app.services.identity.review import crosswalk_summary

        summary = crosswalk_summary(db, org_id, obj.id)
        if summary and summary["sources"]:
            return {"cross_sources": summary}
    except Exception as exc:  # noqa: BLE001 — enrichment must not break reads
        logger.warning("cross_sources enrichment failed for %s: %s",
                       obj.name, exc)
    return {}


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
    from app.services.ontology.permissions import is_write_action

    if is_write_action(action):
        # P0 governance (fail closed): scope check BEFORE any approval is
        # created — an unbound or read-only agent can never originate a write.
        from app.services.ontology.permissions import (
            audit_permission_denial,
            check_action_permission,
        )

        denial = check_action_permission(db, org_id, action, context)
        if denial:
            audit_permission_denial(db, org_id, action, context, denial["reason"])
            return {"success": False, "denied": True, "reason": denial["reason"]}

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

    # P0 review fix: identity claims (caller_kind/authenticated_user_id) are
    # NEVER forwarded to bound tools. Otherwise a read action bound to
    # execute_ontology_action could inherit the console claim and fire a
    # nested WRITE with no approval — nested ontology executes re-enter
    # check_action_permission with no identity and fail closed instead.
    forward_ctx = {
        k: v for k, v in (context or {}).items()
        if k not in ("caller_kind", "authenticated_user_id")
    }
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
                    **forward_ctx,
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

    # P0 governance (fail closed): a valid approval does NOT upgrade the
    # caller's domain scope — the same write predicate applies here.
    from app.services.ontology.permissions import (
        audit_permission_denial,
        check_action_permission,
    )

    denial = check_action_permission(db, org_id, action, context)
    if denial:
        audit_permission_denial(db, org_id, action, context, denial["reason"])
        return {"success": False, "denied": True, "reason": denial["reason"]}

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

    # P0 review fix: the approval is proof for ONE specific write — params
    # may not drift from what was approved. Runtime params that are absent
    # from (or differ with) the approved params are refused honestly.
    approved_params = dict(ctx.get("params") or {})
    if params:
        drift = {
            k: v for k, v in params.items()
            if k not in approved_params or approved_params[k] != v
        }
        if drift:
            return {
                "success": False,
                "reason": (
                    "approval does not cover changed params: "
                    f"{sorted(drift)} — request a new approval"
                ),
            }

    merged = {**approved_params, **(action.params_json or {}),
              **(params or {})}
    import asyncio

    # Identity claims are never forwarded to bound tools (P0 review fix —
    # same rule as the read path in execute_action).
    forward_ctx = {
        k: v for k, v in (context or {}).items()
        if k not in ("caller_kind", "authenticated_user_id")
    }
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
                    **forward_ctx,
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
