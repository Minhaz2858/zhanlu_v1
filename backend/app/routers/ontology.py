"""Ontology router — the org's enterprise profile API (P0-2).

Org-scoped like every surface: reads resolve the caller's org (platform
admin may pass org_id explicitly, 404 otherwise); writes are Org-Admin
only. The overview endpoint lazily seeds the 14 domain packs.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_current_user_required, get_db, require_org_admin
from app.models.user import User
from app.services.ontology import (
    ensure_domains,
    execute_action,
    get_action,
    get_domain,
    get_object,
    get_object_by_name,
    list_actions,
    list_bindings,
    list_domains,
    list_links,
    list_objects,
    read_object_data,
)
from app.services.ontology.data import execute_approved_action
from app.services.ontology.coverage import compute_coverage
from app.services.ontology.discovery import discover_and_recompute_coverage
from app.services.ontology.serialize import (
    action_to_dict,
    binding_to_dict,
    domain_to_dict,
    link_to_dict,
    object_to_dict,
)

router = APIRouter(tags=["ontology"])


def _org_or_404(user: User, org_id: str | None) -> str:
    """Same org-scoping rules as /portfolio: platform admin may query
    another org explicitly; everyone else is pinned to their own."""
    is_platform_admin = user.role == "admin" and user.org_id == "default-org"
    if org_id and is_platform_admin:
        return org_id
    if org_id and org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    return user.org_id


@router.get("/ontology/overview")
def ontology_overview(
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Domains + objects/links/actions counts + coverage — the admin
    Enterprise Profile payload (seeds the packs on first read)."""
    org = _org_or_404(user, org_id)
    ensure_domains(db, org)
    domains = list_domains(db, org)
    objects = list_objects(db, org)
    links = list_links(db, org)
    actions = list_actions(db, org)
    coverage = compute_coverage(db, org)
    cov_by_id = {c["domain_id"]: c for c in coverage}
    return {
        "org_id": org,
        "domains": [
            {**domain_to_dict(d),
             "objects": sum(1 for o in objects if o.domain_id == d.id),
             "coverage": cov_by_id.get(d.id)}
            for d in domains
        ],
        "totals": {
            "domains": len(domains),
            "objects": len(objects),
            "objects_live": sum(c["objects_live"] for c in coverage),
            "actions": len(actions),
            "actions_executable": sum(c["actions_executable"] for c in coverage),
            "links": len(links),
        },
    }


@router.get("/ontology/objects")
def get_objects(
    domain_id: str | None = Query(default=None),
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    org = _org_or_404(user, org_id)
    return [object_to_dict(o) for o in list_objects(db, org, domain_id)]


@router.get("/ontology/links")
def get_links(
    domain_id: str | None = Query(default=None),
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    org = _org_or_404(user, org_id)
    return [link_to_dict(l) for l in list_links(db, org, domain_id)]


@router.get("/ontology/actions")
def get_actions(
    domain_id: str | None = Query(default=None),
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    org = _org_or_404(user, org_id)
    return [action_to_dict(a) for a in list_actions(db, org, domain_id)]


@router.get("/ontology/bindings")
def get_bindings(
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    org = _org_or_404(user, org_id)
    return [binding_to_dict(b) for b in list_bindings(db, org)]


class ObjectBindBody(BaseModel):
    backing_kind: str
    backing_ref: str | None = None
    properties: list | None = None


@router.patch("/ontology/objects/{object_id}/bind")
def bind_object(
    object_id: str,
    body: ObjectBindBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Wire an object to live data (backing_kind=table + table name, or
    entity/metric id). The Coverage Map's honesty depends on this."""
    org = admin.org_id
    obj = get_object(db, org, object_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    if body.backing_kind not in ("table", "entity", "metric", "manual"):
        raise HTTPException(status_code=422, detail="invalid backing_kind")
    obj.backing_kind = body.backing_kind
    obj.backing_ref = body.backing_ref
    if body.properties is not None:
        obj.properties_json = body.properties
    db.commit()
    return object_to_dict(obj)


class ObjectDataBody(BaseModel):
    filters: dict | None = None
    limit: int = 50


@router.post("/ontology/objects/{object_id}/data")
def read_object(
    object_id: str,
    body: ObjectDataBody | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    org = user.org_id
    obj = get_object(db, org, object_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    body = body or ObjectDataBody()
    return read_object_data(db, org, obj, body.filters, body.limit)


class ActionExecuteBody(BaseModel):
    params: dict | None = None


@router.post("/ontology/actions/{action_id}/execute")
def execute_ontology_action(
    action_id: str,
    body: ActionExecuteBody | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    org = user.org_id
    body = body or ActionExecuteBody()
    return execute_action(
        db, org, getattr(user, "id", None), action_id,
        body.params, context={"user_id": getattr(user, "id", None)},
    )


class ActionBindBody(BaseModel):
    tool_name: str | None = None
    description: str | None = None


@router.patch("/ontology/actions/{action_id}/bind")
def bind_action(
    action_id: str,
    body: ActionBindBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Wire an action to a registry tool (the verb becomes executable).

    Coverage treats a bound tool that doesn't exist as not-executable —
    binding is validated against the registry here too."""
    from app.services.tool_registry import registry

    org = admin.org_id
    action = get_action(db, org, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    if body.tool_name is not None:
        if registry.get_entry(body.tool_name) is None:
            raise HTTPException(
                status_code=422,
                detail=f"tool {body.tool_name!r} is not in the registry",
            )
        action.tool_name = body.tool_name
    if body.description is not None:
        action.description = body.description
    db.commit()
    return action_to_dict(action)


class ApprovedExecuteBody(BaseModel):
    approval_id: str
    params: dict | None = None


@router.get("/ontology/po-drafts")
def list_po_drafts(
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """The buyer's worklist: approval-gated PO drafts (P0-4 write-back)."""
    from app.models.procurement import ProcurementPoDraft

    org = _org_or_404(user, org_id)
    rows = (
        db.query(ProcurementPoDraft)
        .filter(
            ProcurementPoDraft.org_id == org,
            ProcurementPoDraft.is_deleted == False,  # noqa: E712
        )
        .order_by(ProcurementPoDraft.created_date.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": d.id, "supplier_name": d.supplier_name,
            "supplier_code": d.supplier_code, "product": d.product,
            "quantity": d.quantity, "unit": d.unit,
            "est_unit_price": d.est_unit_price, "est_amount": d.est_amount,
            "currency": d.currency, "justification": d.justification,
            "status": d.status, "approval_id": d.approval_id,
            "created_date": d.created_date.isoformat() if d.created_date else None,
        }
        for d in rows
    ]


@router.post("/ontology/discover")
def run_discovery(
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Auto-discovery seeding: match unwired ontology objects against the
    org's real database schemas (verified bindings only — never guessed),
    then recompute honest coverage."""
    org = admin.org_id
    ensure_domains(db, org)
    return discover_and_recompute_coverage(db, org)


@router.get("/ontology/po-drafts/export")
def export_po_drafts(
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Export the buyer's PO draft worklist as an xlsx procurement file."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font

    from app.models.procurement import ProcurementPoDraft

    org = _org_or_404(user, org_id)
    rows = (
        db.query(ProcurementPoDraft)
        .filter(
            ProcurementPoDraft.org_id == org,
            ProcurementPoDraft.is_deleted == False,  # noqa: E712
        )
        .order_by(ProcurementPoDraft.created_date.desc())
        .limit(1000)
        .all()
    )
    headers = [
        "Draft ID", "Supplier", "Supplier Code", "Product", "Quantity",
        "Unit", "Est. Unit Price", "Est. Amount", "Currency",
        "Justification", "Status", "Created",
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "PO Drafts"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for d in rows:
        ws.append([
            d.id, d.supplier_name, d.supplier_code, d.product,
            d.quantity, d.unit, d.est_unit_price, d.est_amount,
            d.currency, d.justification, d.status,
            d.created_date.isoformat() if d.created_date else None,
        ])
    # Reasonable column widths for readability in Excel/WPS.
    widths = [40, 28, 14, 30, 10, 8, 14, 14, 10, 44, 12, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="po_drafts.xlsx"'},
    )


class FanoutBody(BaseModel):
    object_name: str
    change_summary: str
    team_id: str | None = None


@router.post("/ontology/fanout/notify")
def fanout_notify(
    body: FanoutBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """P1-3: fan out a business-object change to the agents bound to
    LINKED objects' domains (ontology-link traversal, audit-logged)."""
    from app.services.ontology.fanout import notify_object_change

    return notify_object_change(
        db, admin.org_id, body.object_name, body.change_summary,
        changed_by=getattr(admin, "id", None), team_id=body.team_id,
    )


@router.get("/ontology/coverage-value")
def coverage_value(
    days: int = Query(default=30, ge=1, le=365),
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """P1-4: per-domain value rollups (ROI/usage/acceptance joined onto
    the coverage map via agent↔domain bindings)."""
    from app.services.ontology.domain_value import domain_value_rollups

    org = _org_or_404(user, org_id)
    return domain_value_rollups(db, org, days=days)


@router.get("/ontology/export-template")
def export_template(
    domains: str | None = Query(default=None,
                                description="Comma-separated domain codes"),
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """P1-6: export a deployable template (domain packs + ontology subset
    + bound agents) as a JSON bundle."""
    import json

    from fastapi.responses import Response

    from app.services.ontology.template_export import export_org_template

    org = _org_or_404(admin, org_id)
    codes = [d.strip() for d in (domains or "").split(",") if d.strip()] or None
    bundle = export_org_template(db, org, codes)
    return Response(
        content=json.dumps(bundle, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="org_template.json"'
        },
    )


@router.post("/ontology/actions/{action_id}/execute-approved")
def execute_approved_ontology_action(
    action_id: str,
    body: ApprovedExecuteBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Run a write-type action AFTER an org admin approved it (P0-3).

    The approval is proof for THIS action + tool only — mismatches are
    refused; expiry is enforced."""
    org = user.org_id
    return execute_approved_action(
        db, org, getattr(user, "id", None), action_id,
        body.approval_id, body.params,
        context={"user_id": getattr(user, "id", None)},
    )


class BindingBody(BaseModel):
    agent_name: str
    domain_code: str
    permission_scope: str = "read"


@router.post("/ontology/bindings")
def create_binding(
    body: BindingBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Bind an agent to a domain (agents inherit the domain's objects and
    a permission ceiling: read | write)."""
    if body.permission_scope not in ("read", "write"):
        raise HTTPException(status_code=422, detail="permission_scope must be read|write")
    org = admin.org_id
    ensure_domains(db, org)
    domain = next(
        (d for d in list_domains(db, org) if d.code == body.domain_code), None
    )
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    from app.models.ontology import AgentDomainBinding

    existing = (
        db.query(AgentDomainBinding)
        .filter(
            AgentDomainBinding.org_id == org,
            AgentDomainBinding.agent_name == body.agent_name,
            AgentDomainBinding.domain_id == domain.id,
        )
        .first()
    )
    if existing:
        existing.permission_scope = body.permission_scope
    else:
        db.add(AgentDomainBinding(
            org_id=org, agent_name=body.agent_name,
            domain_id=domain.id, permission_scope=body.permission_scope,
        ))
    db.commit()
    compute_coverage(db, org)
    return {"ok": True, "agent_name": body.agent_name,
            "domain": domain.code, "permission_scope": body.permission_scope}
