"""Org template export (P1-6, IBM gap G14 second half).

A deployable "template" = domain packs + ontology subset + bound agents:
the packaging that makes a vertical implementation (e.g. the
petrochemical trading template) a portable artifact instead of a
consulting project.

Export is a plain JSON bundle — re-importable by a future seed step and
readable by humans. Only live/wired state is exported as-is; draft
objects export too but carry ``backing: null`` so an importing org can
see what must be re-bound to ITS data (bindings are never faked).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.ontology import AgentDomainBinding
from app.services.ontology.queries import (
    list_actions,
    list_bindings,
    list_domains,
    list_links,
    list_objects,
)


def export_org_template(
    db: Session,
    org_id: str,
    domain_codes: list[str] | None = None,
) -> dict:
    """Build the template bundle for (optionally a subset of) the org's
    domains, plus the agents bound to them."""
    domains = list_domains(db, org_id)
    if domain_codes:
        wanted = set(domain_codes)
        domains = [d for d in domains if d.code in wanted]
    objects = list_objects(db, org_id)
    actions = list_actions(db, org_id)
    links = list_links(db, org_id)
    bindings = list_bindings(db, org_id)
    obj_domain = {o.id: o.domain_id for o in objects}

    dom_ids = {d.id for d in domains}
    installed_packs = []
    try:
        from app.models.forecasting import DomainPackInstall

        installed_packs = [
            i.pack_key for i in db.query(DomainPackInstall)
            .filter(DomainPackInstall.org_id == org_id)
            .all()
        ]
    except Exception:  # noqa: BLE001 — export continues without pack info
        installed_packs = []

    out_domains = []
    bound_agents: set[str] = set()
    for d in domains:
        dom_objects = [o for o in objects if o.domain_id == d.id]
        dom_actions = [a for a in actions if obj_domain.get(a.object_id) == d.id
                       and a.object_id]
        out_domains.append({
            "code": d.code,
            "name": d.name,
            "description": d.description,
            "objects": [
                {
                    "name": o.name,
                    "display_name": o.display_name,
                    "description": o.description,
                    "backing": (
                        {"kind": o.backing_kind, "ref": o.backing_ref}
                        if o.backing_ref and o.backing_kind != "manual"
                        else None
                    ),
                    "properties": o.properties_json or [],
                }
                for o in dom_objects
            ],
            "actions": [
                {
                    "name": a.name, "description": a.description,
                    "object": next(
                        (o.name for o in dom_objects if o.id == a.object_id), None),
                    "tool_name": a.tool_name, "risk_level": a.risk_level,
                    "params": a.params_json or {},
                }
                for a in dom_actions
            ],
        })

    agent_bindings = {}
    for b in bindings:
        if b.domain_id in dom_ids:
            agent_bindings.setdefault(b.agent_name, []).append({
                "domain": next(
                    (d.code for d in domains if d.id == b.domain_id), None),
                "permission_scope": b.permission_scope,
            })
            bound_agents.add(b.agent_name)

    return {
        "template_version": "1.0.0",
        "exported_from_org": org_id,
        "installed_packs": sorted(set(installed_packs)),
        "domains": out_domains,
        "links": [
            {
                "from": next((o.name for o in objects if o.id == l.from_object_id), None),
                "to": next((o.name for o in objects if o.id == l.to_object_id), None),
                "link_type": l.link_type, "cardinality": l.cardinality,
            }
            for l in links
            if l.from_object_id in dom_ids or l.to_object_id in dom_ids
        ],
        "agent_bindings": agent_bindings,
        "note": (
            "backing refs are org-specific (kb:<id>:<table>) — an importing "
            "org must re-run discovery to bind objects to ITS OWN data"
        ),
    }
