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

from app.models.ontology import (
    AgentDomainBinding,
    OntologyDomain,
    OntologyLink,
    OntologyObject,
)
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


def import_org_template(db: Session, org_id: str, bundle: dict) -> dict:
    """Seed an org from an exported template bundle (P1-6 counterpart).

    HONESTY RULES (mirroring discovery):
    - never overwrites: existing domains/objects/links/bindings are kept
    - imported objects arrive UNBOUND (backing null) — the importing org
      re-runs discovery to bind them to ITS OWN data; bundle backing refs
      are never copied (they point at the exporting org's databases)
    - idempotent: importing the same bundle twice changes nothing
    """
    report = {
        "domains_created": 0, "objects_created": 0, "objects_skipped": 0,
        "links_created": 0, "links_skipped": 0,
        "bindings_created": 0, "bindings_skipped": 0,
    }
    existing_domains = {d.code: d for d in list_domains(db, org_id)}
    existing_objects = {o.name: o for o in list_objects(db, org_id)}
    existing_links = {
        (l.from_object_id, l.to_object_id, l.link_type)
        for l in list_links(db, org_id)
    }

    for dspec in (bundle.get("domains") or []):
        code = (dspec.get("code") or "").strip()
        if not code:
            continue
        domain = existing_domains.get(code)
        if not domain:
            domain = OntologyDomain(
                org_id=org_id, code=code,
                name=dspec.get("name") or code,
                description=dspec.get("description"),
                sort_order=100,
            )
            db.add(domain)
            db.flush()
            existing_domains[code] = domain
            report["domains_created"] += 1

        for ospec in (dspec.get("objects") or []):
            name = (ospec.get("name") or "").strip()
            if not name:
                continue
            if name in existing_objects:
                report["objects_skipped"] += 1
                continue
            props = []
            for p in (ospec.get("properties") or []):
                if isinstance(p, dict) and p.get("name"):
                    # backing columns are NOT imported — org-specific
                    props.append({"name": p["name"],
                                  "type": p.get("type") or "string"})
            obj = OntologyObject(
                org_id=org_id, domain_id=domain.id, name=name,
                display_name=ospec.get("display_name") or name,
                description=ospec.get("description"),
                backing_kind="manual", backing_ref=None,
                properties_json=props,
            )
            db.add(obj)
            db.flush()
            existing_objects[name] = obj
            report["objects_created"] += 1

    obj_by_name = {o.name: o for o in list_objects(db, org_id)}
    for lspec in (bundle.get("links") or []):
        src = obj_by_name.get((lspec.get("from") or ""))
        dst = obj_by_name.get((lspec.get("to") or ""))
        link_type = lspec.get("link_type") or "related_to"
        if not src or not dst or src.id == dst.id:
            continue
        key = (src.id, dst.id, link_type)
        if key in existing_links:
            report["links_skipped"] += 1
            continue
        db.add(OntologyLink(
            org_id=org_id, from_object_id=src.id, to_object_id=dst.id,
            link_type=link_type, cardinality=lspec.get("cardinality") or "N:M",
        ))
        existing_links.add(key)
        report["links_created"] += 1

    for agent_name, binds in (bundle.get("agent_bindings") or {}).items():
        for b in binds:
            domain = existing_domains.get(b.get("domain") or "")
            if not domain or not agent_name:
                continue
            exists = any(
                bd.agent_name == agent_name and bd.domain_id == domain.id
                for bd in list_bindings(db, org_id)
            )
            if exists:
                report["bindings_skipped"] += 1
                continue
            db.add(AgentDomainBinding(
                org_id=org_id, agent_name=agent_name,
                domain_id=domain.id,
                permission_scope=b.get("permission_scope") or "read",
            ))
            report["bindings_created"] += 1

    db.commit()
    try:
        from app.services.ontology.coverage import compute_coverage

        compute_coverage(db, org_id)
    except Exception:  # noqa: BLE001 — coverage recompute is a follow-up
        pass
    return report
