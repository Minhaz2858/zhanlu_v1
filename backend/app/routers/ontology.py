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
    return [object_to_dict(o, doc_ctx={"db": db, "user": user})
            for o in list_objects(db, org, domain_id)]


@router.get("/ontology/links")
def get_links(
    domain_id: str | None = Query(default=None),
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    org = _org_or_404(user, org_id)
    return [link_to_dict(l) for l in list_links(db, org, domain_id)]


METRIC_GRAPH_LIMIT = 200


@router.get("/ontology/graph")
def ontology_graph(
    domain_id: str | None = Query(default=None),
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """P7 — aggregate graph payload for the Ontology Explorer. One
    READ-ONLY call: domains + objects (per-viewer document filtering,
    exactly like /ontology/objects) + links + agent bindings + governed
    metric definitions (ALL lifecycles — governance state must be
    visible, never implied published)."""
    from app.models.metric_definition import MetricDefinition
    from app.services.ontology.metric_dependencies import build_dependency_edges

    org = _org_or_404(user, org_id)
    domains = list_domains(db, org)
    objects = list_objects(db, org, domain_id)
    links = list_links(db, org, domain_id)
    metrics = (
        db.query(MetricDefinition)
        .filter(MetricDefinition.org_id == org,
                MetricDefinition.is_deleted == False)  # noqa: E712
        .order_by(MetricDefinition.name)
        .limit(METRIC_GRAPH_LIMIT + 1)
        .all()
    )
    metrics_truncated = len(metrics) > METRIC_GRAPH_LIMIT
    metrics = metrics[:METRIC_GRAPH_LIMIT]
    # D3 — explicit metric-to-metric dependency edges (cycles dropped, surfaced)
    metric_deps = build_dependency_edges(db, org)
    return {
        "org_id": org,
        "domains": [
            {"id": d.id, "code": d.code, "name": d.name,
             "object_count": sum(1 for o in objects
                                 if o.domain_id == d.id)}
            for d in domains
        ],
        "objects": [object_to_dict(o, doc_ctx={"db": db, "user": user})
                    for o in objects],
        "links": [link_to_dict(l) for l in links],
        "bindings": [binding_to_dict(b) for b in list_bindings(db, org)],
        "metrics": [
            {"id": m.id, "name": m.name,
             "lifecycle": m.lifecycle, "version": m.version,
             "evidence_status": m.evidence_status,
             "reviewed_by": m.reviewed_by,
             "base_table": m.base_table,
             "datasource_id": m.datasource_id,
             "aggregation": m.aggregation, "unit": m.unit,
             "depends_on_metrics": m.depends_on_metrics or []}
            for m in metrics
        ],
        # D3 — dependency edges for the graph renderer (from=dependent)
        "metric_dependencies": metric_deps,
        # governance honesty: never silently drop metrics past the cap
        "metrics_truncated": metrics_truncated,
    }


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
    # P5-T2 (bind trap): a binding the runtime cannot serve is refused at
    # the door — accepting it would create a permanent-refusal object.
    if body.backing_kind == "metric":
        from app.models.metric_definition import MetricDefinition

        metric = (
            db.query(MetricDefinition)
            .filter(
                MetricDefinition.id == (body.backing_ref or "").strip(),
                MetricDefinition.org_id == org,
                MetricDefinition.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if metric is None:
            raise HTTPException(status_code=400, detail=(
                "metric backing_ref does not resolve to a metric definition "
                "in this org"))
        if not metric.is_enabled or (metric.lifecycle or "") != "published":
            raise HTTPException(status_code=400, detail=(
                f"metric {metric.name!r} is {metric.lifecycle or 'disabled'} "
                "— only published, enabled metrics can back an object"))
    if body.backing_kind == "entity":
        from app.models.identity import IdentityMatch

        confirmed = (
            db.query(IdentityMatch)
            .filter(
                IdentityMatch.org_id == org,
                IdentityMatch.object_id == obj.id,
                IdentityMatch.status == "confirmed",
                IdentityMatch.is_deleted == False,  # noqa: E712
            )
            .count()
        )
        if not confirmed:
            raise HTTPException(status_code=400, detail=(
                "entity backing requires at least one CONFIRMED identity "
                "crosswalk for this object — confirm one in the identity "
                "review queue first"))
    obj.backing_kind = body.backing_kind
    obj.backing_ref = body.backing_ref
    if body.properties is not None:
        obj.properties_json = body.properties
    # P1 evidence taxonomy: explicit human authorization is the ONLY path
    # to 'verified'; unbinding returns the object to 'proposed'.
    from app.services.ontology.evidence import stamp_human_bind, stamp_unbind

    if body.backing_kind == "manual" or not (body.backing_ref or "").strip():
        obj.backing_ref = None  # no dangling ref on an unbound proposal
        stamp_unbind(obj)
    else:
        stamp_human_bind(obj, bound_by=getattr(admin, "email", None)
                         or str(getattr(admin, "id", "") or ""))
    # P2: exactly one revision for the bind/unbind, atomic with the mutation
    from app.services.ontology.revisions import record_revision

    record_revision(
        db, "object", obj,
        change_kind="unbind" if obj.evidence_status == "proposed" and
        obj.backing_kind == "manual" else "bind",
        changed_by=getattr(admin, "email", None)
        or str(getattr(admin, "id", "") or ""),
    )
    db.commit()
    return object_to_dict(obj, doc_ctx={"db": db, "user": admin})


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
        body.params, context={"user_id": getattr(user, "id", None),
                              "authenticated_user_id": str(getattr(user, "id", "") or ""),
                              "caller_kind": "console_user"},
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
    # P1 evidence taxonomy: wiring/unwiring the tool updates the action's
    # evidence status (inferred when executable, proposed when not).
    from app.services.ontology.evidence import stamp_action_binding

    stamp_action_binding(action, bound_by=getattr(admin, "email", None)
                         or str(getattr(admin, "id", "") or ""))
    from app.services.ontology.revisions import record_revision

    record_revision(
        db, "action", action, change_kind="update",
        changed_by=getattr(admin, "email", None)
        or str(getattr(admin, "id", "") or ""),
    )
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


class SensorBody(BaseModel):
    name: str
    object_name: str
    kb_id: str
    watch_sql: str
    op: str = "lt"
    threshold: float = 0
    schedule_seconds: int = 3600
    description: str | None = None
    team_id: str | None = None


@router.post("/ontology/sensors")
def create_sensor(
    body: SensorBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """P3-2: create a scheduled data watch that fans out through the
    ontology on breach. The watch SQL runs through the read-only gate."""
    import uuid as _uuid

    from app.models.ontology_sensor import OntologySensor

    if body.op not in ("lt", "lte", "gt", "gte", "eq"):
        raise HTTPException(422, f"unknown operator: {body.op}")
    if body.schedule_seconds < 60:
        raise HTTPException(422, "schedule_seconds must be >= 60")
    sensor = OntologySensor(
        id=str(_uuid.uuid4()), org_id=admin.org_id,
        name=body.name, description=body.description,
        object_name=body.object_name, kb_id=body.kb_id,
        watch_sql=body.watch_sql, op=body.op, threshold=body.threshold,
        schedule_seconds=body.schedule_seconds, team_id=body.team_id,
        enabled=True, created_by_id=getattr(admin, "id", None),
    )
    db.add(sensor)
    db.commit()
    return {"id": sensor.id, "status": "created"}


@router.get("/ontology/sensors")
def list_sensors(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """P3-2: list the org's sensors with last state + recent runs, plus
    the bindable object names and data sources for the create form."""
    from app.models.knowledge_base import KnowledgeBase
    from app.models.ontology_sensor import OntologySensor, OntologySensorRun

    org = getattr(user, "org_id", None) or "default-org"
    sensors = (
        db.query(OntologySensor)
        .filter(OntologySensor.org_id == org,
                OntologySensor.is_deleted == False)  # noqa: E712
        .order_by(OntologySensor.created_date.desc())
        .all()
    )
    out = []
    for s in sensors:
        d = s.to_dict()
        d.pop("watch_sql", None)
        last_runs = (
            db.query(OntologySensorRun)
            .filter(OntologySensorRun.sensor_id == s.id,
                    OntologySensorRun.is_deleted == False)  # noqa: E712
            .order_by(OntologySensorRun.created_date.desc())
            .limit(5).all()
        )
        d["recent_runs"] = [
            {"value": r.value, "breached": r.breached,
             "transition": r.transition, "error": r.error,
             "ran_at": r.created_date.isoformat() if r.created_date else None}
            for r in last_runs
        ]
        out.append(d)
    kbs = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.org_id == org,
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.source_kind == "database")  # noqa: E712
        .all()
    )
    objects = [
        {"name": o.name, "display_name": o.display_name}
        for o in list_objects(db, org)
    ]
    return {
        "sensors": out,
        "data_sources": [{"id": k.id, "name": k.name, "db_type": k.db_type}
                         for k in kbs],
        "objects": objects,
    }


@router.post("/ontology/sensors/{sensor_id}/run")
def run_sensor_now(
    sensor_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """P3-2: immediate evaluation (test run / manual run)."""
    from app.models.ontology_sensor import OntologySensor
    from app.services.ontology.sensing import evaluate_sensor

    sensor = db.query(OntologySensor).filter(
        OntologySensor.id == sensor_id,
        OntologySensor.org_id == admin.org_id,
        OntologySensor.is_deleted == False,  # noqa: E712
    ).first()
    if not sensor:
        raise HTTPException(404, "sensor not found")
    return evaluate_sensor(db, sensor)


@router.delete("/ontology/sensors/{sensor_id}")
def delete_sensor(
    sensor_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    from app.models.ontology_sensor import OntologySensor

    sensor = db.query(OntologySensor).filter(
        OntologySensor.id == sensor_id,
        OntologySensor.org_id == admin.org_id,
        OntologySensor.is_deleted == False,  # noqa: E712
    ).first()
    if not sensor:
        raise HTTPException(404, "sensor not found")
    sensor.is_deleted = True
    sensor.enabled = False
    db.commit()
    return {"id": sensor_id, "status": "deleted"}


class ResolveBody(BaseModel):
    note: str | None = None


@router.get("/ontology/escalations")
def list_escalations_ep(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """P3-6: the human handoff queue (escalations with full context)."""
    from app.services.ontology.escalation import list_escalations

    org = getattr(user, "org_id", None) or "default-org"
    return list_escalations(db, org, status=status)


@router.post("/ontology/escalations/{escalation_id}/resolve")
def resolve_escalation_ep(
    escalation_id: str,
    body: ResolveBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """P3-6: close the loop — a human resolved the handoff (audited)."""
    from app.services.ontology.escalation import resolve_escalation

    try:
        return resolve_escalation(
            db, admin.org_id, escalation_id,
            resolved_by=getattr(admin, "id", None), note=body.note,
        )
    except LookupError as e:
        raise HTTPException(404, str(e))


class ImportTemplateBody(BaseModel):
    bundle: dict


@router.post("/ontology/import-template")
def import_template(
    body: ImportTemplateBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """P1-6 counterpart: seed this org from an exported template bundle
    (domains + unbound objects + links + agent bindings). Idempotent;
    objects arrive unbound — run discovery to bind to YOUR data."""
    from app.services.ontology.template_export import import_org_template

    bundle = body.bundle
    if not isinstance(bundle, dict) or not bundle.get("domains"):
        raise HTTPException(422, "bundle must be an exported template JSON with 'domains'")
    return import_org_template(db, admin.org_id, bundle)


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
        context={"user_id": getattr(user, "id", None),
                 "authenticated_user_id": str(getattr(user, "id", "") or ""),
                 "caller_kind": "console_user"},
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




# ── P2: versioned ontology registry — history, rollback, retirement ────


def _scrub_revision(r: dict, db: Session, user: User) -> dict:
    """D7 (P4 T8): revision snapshots embed provenance_json, which may
    carry document-derived entries (quotes, locations, reviewer ids).
    Apply the SAME per-entry D6 filter to the snapshots that the live
    object serialization uses — the revision feed must not become a
    provenance side-channel. Before/after are reassigned, never mutated."""
    from app.services.ontology.doc_access import filter_document_entries

    for key in ("before", "after"):
        snap = r.get(key)
        if not isinstance(snap, dict):
            continue
        prov = snap.get("provenance_json")
        if isinstance(prov, dict) and "document_provenance" in prov:
            snap = dict(snap)
            snap["provenance_json"] = filter_document_entries(
                db, user, prov)
            r[key] = snap
    return r


@router.get("/ontology/revisions")
def list_all_revisions(
    since: str | None = None,
    entity_type: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Org-wide revision feed (newest first). `since` = a revision id.
    Document provenance entries inside snapshots are per-entry gated (D7)."""
    from app.services.ontology.revisions import revision_to_dict, revisions_since

    if entity_type and entity_type not in ("object", "link", "action", "domain"):
        raise HTTPException(status_code=422, detail="invalid entity_type")
    rows = revisions_since(db, user.org_id, since, entity_type)
    return {"revisions": [
        _scrub_revision(revision_to_dict(r), db, user) for r in rows]}


@router.get("/ontology/{entity_type}/{entity_id}/revisions")
def list_entity_revisions(
    entity_type: str,
    entity_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Per-entity revision history (newest first). Document provenance
    entries inside snapshots are per-entry gated (D7)."""
    from app.services.ontology.revisions import list_revisions, revision_to_dict

    if entity_type not in ("object", "link", "action", "domain"):
        raise HTTPException(status_code=422, detail="invalid entity_type")
    rows = list_revisions(db, user.org_id, entity_type, entity_id)
    return {"revisions": [
        _scrub_revision(revision_to_dict(r), db, user) for r in rows]}


class RollbackBody(BaseModel):
    revision_no: int


def _entity_for_rollback(db: Session, org_id: str, entity_type: str, entity_id: str):
    from app.models.ontology import OntologyAction, OntologyLink, OntologyObject

    model = {
        "object": OntologyObject, "link": OntologyLink,
        "action": OntologyAction,
    }.get(entity_type)
    if model is None:
        raise HTTPException(status_code=422, detail="invalid entity_type")
    entity = (
        db.query(model)
        .filter(model.org_id == org_id, model.id == entity_id)
        .first()
    )
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.post("/ontology/{entity_type}/{entity_id}/rollback")
def rollback_entity(
    entity_type: str,
    entity_id: str,
    body: RollbackBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Restore an element to its state at `revision_no`. Appends a NEW
    `rollback` revision — history is never erased."""
    from app.services.ontology.revisions import (
        rollback_to_revision,
        revision_to_dict,
    )

    entity = _entity_for_rollback(db, admin.org_id, entity_type, entity_id)
    try:
        rev = rollback_to_revision(
            db, entity_type, entity, body.revision_no,
            changed_by=getattr(admin, "email", None)
            or str(getattr(admin, "id", "") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    if rev is None:
        # Requested revision's state == current state: nothing to restore.
        return {"ok": True, "revision": None,
                "note": "state already matches the requested revision — "
                        "no revision recorded"}
    return {"ok": True, "revision": revision_to_dict(rev)}


def _retire(db: Session, admin: User, entity_type: str, entity) -> dict:
    from app.services.ontology.revisions import record_revision

    if entity_type == "action":
        entity.is_active = False
    entity.is_deleted = True
    record_revision(
        db, entity_type, entity, change_kind="retire",
        changed_by=getattr(admin, "email", None)
        or str(getattr(admin, "id", "") or ""),
    )
    db.commit()
    return {"ok": True, "retired": entity.id}


@router.delete("/ontology/objects/{object_id}")
def retire_object(
    object_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Soft-retire an object (history survives; a `retire` revision is
    recorded). Restore via the rollback endpoint."""
    obj = get_object(db, admin.org_id, object_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    return _retire(db, admin, "object", obj)


@router.delete("/ontology/links/{link_id}")
def retire_link(
    link_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    from app.models.ontology import OntologyLink

    link = (
        db.query(OntologyLink)
        .filter(OntologyLink.org_id == admin.org_id,
                OntologyLink.id == link_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    return _retire(db, admin, "link", link)


@router.delete("/ontology/actions/{action_id}")
def retire_action(
    action_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    action = get_action(db, admin.org_id, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    return _retire(db, admin, "action", action)


# ── P3: cross-source identity resolution — review queue API ────────────


class IdentitySourceModel(BaseModel):
    kind: str  # "db" | "file"
    kb_id: str
    table: str | None = None       # db sources
    sheet: str | None = None       # file sources
    column: str | None = None
    pk_column: str | None = None


class IdentityResolveBody(BaseModel):
    object_id: str
    source_a: IdentitySourceModel
    source_b: IdentitySourceModel
    attribute_pairs: list[tuple[str, str]] | None = None
    max_rows: int = 500


def _identity_source_dict(s: IdentitySourceModel) -> dict:
    if s.kind not in ("db", "file"):
        raise HTTPException(status_code=422,
                            detail="source.kind must be db or file")
    if not s.column:
        raise HTTPException(status_code=422,
                            detail="source.column is required")
    d = {"kind": s.kind, "kb_id": s.kb_id, "column": s.column}
    if s.kind == "db":
        if not s.table:
            raise HTTPException(status_code=422,
                                detail="db sources need a table")
        d["table"] = s.table
    else:
        d["sheet"] = s.sheet or ""
    d["pk_column"] = s.pk_column or s.column
    return d


@router.post("/ontology/identity/resolve")
def identity_resolve(
    body: IdentityResolveBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Run tiers 1-4 across two sources for ONE ontology object.
    Bounded, read-only. ALL tiers land as `inferred` (system evidence —
    tiers 1-2 with high confidence); only an org-admin confirm activates
    a crosswalk for the runtime."""
    from app.services.identity.resolver import resolve_object_identity

    report = resolve_object_identity(
        db, admin.org_id, body.object_id,
        _identity_source_dict(body.source_a),
        _identity_source_dict(body.source_b),
        attribute_pairs=body.attribute_pairs,
        max_rows=max(1, min(body.max_rows, 5000)),
    )
    db.commit()
    return report


@router.get("/ontology/identity/matches")
def identity_matches(
    object_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """The org's crosswalk review queue. status: confirmed|inferred|rejected."""
    from app.services.identity.review import list_matches

    if status and status not in ("confirmed", "inferred", "rejected"):
        raise HTTPException(status_code=422, detail="invalid status")
    return list_matches(db, user.org_id, object_id=object_id, status=status)


def _identity_review(match_id: str, decision: str, db: Session,
                     admin: User) -> dict:
    from app.services.identity.review import review_match

    try:
        return review_match(db, admin.org_id, match_id, decision,
                            reviewed_by=getattr(admin, "email", None)
                            or str(getattr(admin, "id", "") or ""))
    except LookupError:
        raise HTTPException(status_code=404, detail="Match not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/ontology/identity/matches/{match_id}/confirm")
def identity_confirm(
    match_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Org-admin confirms a crosswalk (activates it for the runtime).
    Audited as `identity_review`."""
    return _identity_review(match_id, "confirm", db, admin)


@router.post("/ontology/identity/matches/{match_id}/reject")
def identity_reject(
    match_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Org-admin rejects a crosswalk. Audited; the rejection is
    remembered and the pair is never re-proposed."""
    return _identity_review(match_id, "reject", db, admin)


@router.get("/ontology/identity/matches/{match_id}/history")
def identity_history(
    match_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Append-only decision history for one crosswalk (created, confirm,
    reject, reversals) — auditable and reconstructable."""
    from app.services.identity.review import decision_history

    try:
        return decision_history(db, user.org_id, match_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Match not found")


# ── P6: continuous evolution — admin reconciliation ──────────────────────


@router.post("/ontology/evolution/reconcile")
def reconcile_ontology(
    body: dict | None = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Manual administrator reconciliation (P6-T5). apply=false → report
    only; apply=true → the E2-allowed controlled mutations (stale marking
    with revisions, notifications)."""
    from app.services.ontology.evolution import reconcile

    apply = bool((body or {}).get("apply"))
    # Manual admin runs bypass the failed-source retry cap — the cap only
    # exists to stop the SCHEDULED loop from hammering a dead source.
    return reconcile(db, admin.org_id, apply=apply, respect_cap=False)


@router.get("/ontology/evolution/state")
def evolution_state(
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Per-source evolution ledger for this org."""
    from app.models.ontology import OntologyEvolutionState

    rows = (
        db.query(OntologyEvolutionState)
        .filter(OntologyEvolutionState.org_id == admin.org_id,
                OntologyEvolutionState.is_deleted == False)  # noqa: E712
        .order_by(OntologyEvolutionState.source_key)
        .all()
    )
    return {"sources": [{
        "source_key": r.source_key, "source_kind": r.source_kind,
        "signature": r.signature, "last_status": r.last_status,
        "last_error": r.last_error, "attempts": r.attempts,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
    } for r in rows]}


@router.get("/ontology/domains")
def list_domains_ep(
    org_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """The org's domain list (P4 T9: the Doc Evidence review UI needs the
    domain taxonomy for the create-path approval picker; /overview seeds
    packs as a side effect and is too heavy for a picker)."""
    org = _org_or_404(user, org_id)
    return {"domains": [domain_to_dict(d) for d in list_domains(db, org)]}


# ── P4 T8: document-grounded ontology evidence (review queue) ────────────

def _document_gate(db: Session, user: User, kb_id: str):
    """D6 fail-closed gate on the SOURCE document. admin ≠ document-reader:
    org-admin grants the ROUTE, this gate grants the DOCUMENT — approval
    requires BOTH. Returns the KB on success (PermissionError → 403)."""
    from app.models.knowledge_base import KnowledgeBase
    from app.services.ontology.doc_access import assert_document_kb_access

    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id)
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="Source document not found")
    try:
        assert_document_kb_access(db, user, kb)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return kb


@router.post("/ontology/document-evidence/extract")
def extract_document_evidence_ep(
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Run the document→ontology proposal pipeline for one file KB (P4 T6).
    Org-admin + D6 document access required; the master flag gates it."""
    import app.config as config
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    if not getattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED", False):
        raise HTTPException(
            status_code=400,
            detail="document grounding is disabled ("
                   "ONTOLOGY_DOC_GROUNDING_ENABLED)",
        )
    kb = _document_gate(db, admin, str(body.get("kb_id") or ""))
    return extract_document_evidence(db, kb, org_id=admin.org_id)


def _gate_proposal(db: Session, admin: User, proposal_id: str):
    """Fetch an org-scoped proposal AND gate its source document (D6)."""
    from app.models.ontology import DocumentProposal

    p = (
        db.query(DocumentProposal)
        .filter(DocumentProposal.id == proposal_id,
                DocumentProposal.org_id == admin.org_id)
        .first()
    )
    if p is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    _document_gate(db, admin, p.source_kb_id)
    return p


@router.get("/ontology/document-evidence")
def list_document_evidence(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Org review queue. Restricted-source proposals are HIDDEN from admins
    without document access (D6) — the count is reported honestly."""
    from app.models.ontology import DocumentProposal
    from app.services.ontology.doc_access import document_access_state
    from app.services.ontology.document_review import _to_dict

    q = db.query(DocumentProposal).filter(
        DocumentProposal.org_id == admin.org_id)
    if status:
        q = q.filter(DocumentProposal.status == status)
    rows = q.order_by(DocumentProposal.created_date.desc()).all()

    out, hidden = [], 0
    for p in rows:
        from app.models.knowledge_base import KnowledgeBase

        kb = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.id == p.source_kb_id)
            .first()
        )
        if document_access_state(db, admin, kb)["allowed"]:
            out.append(_to_dict(p))
        else:
            hidden += 1
    return {"proposals": out, "hidden": hidden}


@router.post("/ontology/document-evidence/{proposal_id}/approve")
def approve_document_evidence(
    proposal_id: str,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """ATOMIC approve (P4 T7/D4): element + provenance entry + D2 stamp +
    revision + decision event + audit + status. The reviewer identity is
    the authenticated principal — never a request body."""
    from app.services.ontology.document_review import review_proposal

    _gate_proposal(db, admin, proposal_id)
    try:
        return review_proposal(
            db, admin.org_id, proposal_id, "approve",
            reviewed_by=admin.id,
            domain_id=body.get("domain_id"),
            payload_override=body.get("payload_override"),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/ontology/document-evidence/{proposal_id}/reject")
def reject_document_evidence(
    proposal_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    from app.services.ontology.document_review import review_proposal

    _gate_proposal(db, admin, proposal_id)
    try:
        return review_proposal(db, admin.org_id, proposal_id, "reject",
                               reviewed_by=admin.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/ontology/document-evidence/{proposal_id}/revise")
def revise_document_evidence(
    proposal_id: str,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Admin edits the proposal payload while it is still `proposed`."""
    from app.services.ontology.document_review import revise_proposal

    _gate_proposal(db, admin, proposal_id)
    try:
        return revise_proposal(db, admin.org_id, proposal_id,
                               body.get("payload") or {},
                               reviewed_by=admin.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/ontology/document-evidence/{proposal_id}/history")
def document_evidence_history(
    proposal_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Append-only decision history for one proposal (restricted sources
    stay hidden behind the D6 gate)."""
    from app.services.ontology.document_review import decision_history

    _gate_proposal(db, admin, proposal_id)
    try:
        return decision_history(db, admin.org_id, proposal_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Proposal not found")
