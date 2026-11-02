"""Ontology queries — seeding + list/get (P0-2)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.ontology import (
    AgentDomainBinding,
    OntologyAction,
    OntologyDomain,
    OntologyLink,
    OntologyObject,
)
from app.services.ontology.packs import DOMAIN_PACKS


def ensure_domains(db: Session, org_id: str) -> dict:
    """Idempotently seed the 14 domain packs (+ starter contents) per org."""
    existing = {
        d.code
        for d in db.query(OntologyDomain)
        .filter(OntologyDomain.org_id == org_id,
                OntologyDomain.is_deleted == False)  # noqa: E712
        .all()
    }
    created = 0
    created_entities: list[tuple[str, object]] = []
    for pack in DOMAIN_PACKS:
        if pack["code"] in existing:
            continue
        domain = OntologyDomain(
            org_id=org_id, code=pack["code"], name=pack["name"],
            description=pack.get("description"), sort_order=pack.get("sort", 100),
        )
        db.add(domain)
        db.flush()
        created_entities.append(("domain", domain))
        created_entities.extend(
            _seed_pack_contents(db, org_id, domain, pack))
        created += 1
    # P2: birth revisions for every seeded element (idempotent re-seeds
    # produce zero — only newly created entities are recorded here).
    from app.services.ontology.revisions import record_revision

    for entity_type, entity in created_entities:
        record_revision(db, entity_type, entity, change_kind="create",
                        changed_by="seed")
    db.commit()
    return {"created": created, "existing": len(existing)}


def _seed_pack_contents(db: Session, org_id: str, domain: OntologyDomain,
                        pack: dict) -> list[tuple[str, object]]:
    created: list[tuple[str, object]] = []
    obj_by_name: dict[str, OntologyObject] = {}
    for spec in pack.get("objects", []):
        obj = OntologyObject(
            org_id=org_id, domain_id=domain.id, name=spec["name"],
            display_name=spec.get("display") or spec["name"],
            description=spec.get("description"),
            backing_kind="manual", backing_ref=None,
            properties_json=[{"name": p, "type": "string"}
                             for p in spec.get("properties", [])],
        )
        db.add(obj)
        obj_by_name[spec["name"]] = obj
        created.append(("object", obj))
    db.flush()

    for from_name, to_name, link_type in pack.get("links", []):
        src, dst = obj_by_name.get(from_name), obj_by_name.get(to_name)
        if src and dst:
            link = OntologyLink(org_id=org_id, from_object_id=src.id,
                                to_object_id=dst.id, link_type=link_type)
            db.add(link)
            created.append(("link", link))

    for spec in pack.get("objects", []):
        obj = obj_by_name[spec["name"]]
        for act in spec.get("actions", []):
            risk = act.get("risk", "read")
            action = OntologyAction(
                org_id=org_id, object_id=obj.id, name=act["name"],
                tool_name=act.get("tool"), risk_level=risk,
                requires_confirmation=(risk == "write"),
            )
            db.add(action)
            created.append(("action", action))
    db.flush()
    return created


def _domain_object_ids(db: Session, org_id: str, domain_id: str) -> list[str]:
    return [o.id for o in db.query(OntologyObject.id).filter(
        OntologyObject.org_id == org_id,
        OntologyObject.domain_id == domain_id).all()]


def list_domains(db: Session, org_id: str) -> list[OntologyDomain]:
    return (db.query(OntologyDomain)
            .filter(OntologyDomain.org_id == org_id,
                    OntologyDomain.is_deleted == False)  # noqa: E712
            .order_by(OntologyDomain.sort_order).all())


def list_objects(db: Session, org_id: str, domain_id: str | None = None) -> list:
    q = db.query(OntologyObject).filter(
        OntologyObject.org_id == org_id, OntologyObject.is_deleted == False)  # noqa: E712
    if domain_id:
        q = q.filter(OntologyObject.domain_id == domain_id)
    return q.order_by(OntologyObject.name).all()


def list_links(db: Session, org_id: str, domain_id: str | None = None) -> list:
    # Phase 7C (D5): retired links honor soft-delete on reads — before 7C
    # this list returned explicitly retired links (the drilldown filtered
    # them, this list did not).
    q = db.query(OntologyLink).filter(
        OntologyLink.org_id == org_id,
        OntologyLink.is_deleted == False)  # noqa: E712
    if domain_id:
        ids = _domain_object_ids(db, org_id, domain_id)
        q = q.filter(OntologyLink.from_object_id.in_(ids)
                     | OntologyLink.to_object_id.in_(ids))
    return q.all()


def list_actions(db: Session, org_id: str, domain_id: str | None = None) -> list:
    q = db.query(OntologyAction).filter(
        OntologyAction.org_id == org_id,
        OntologyAction.is_deleted == False)  # noqa: E712
    if domain_id:
        q = q.filter(OntologyAction.object_id.in_(
            _domain_object_ids(db, org_id, domain_id)))
    return q.all()


def list_bindings(db: Session, org_id: str, domain_id: str | None = None) -> list:
    q = db.query(AgentDomainBinding).filter(AgentDomainBinding.org_id == org_id)
    if domain_id:
        q = q.filter(AgentDomainBinding.domain_id == domain_id)
    return q.all()


def get_domain(db: Session, org_id: str, domain_id: str) -> OntologyDomain | None:
    return db.query(OntologyDomain).filter(
        OntologyDomain.id == domain_id, OntologyDomain.org_id == org_id,
        OntologyDomain.is_deleted == False).first()  # noqa: E712


def get_object(db: Session, org_id: str, object_id: str) -> OntologyObject | None:
    return db.query(OntologyObject).filter(
        OntologyObject.id == object_id, OntologyObject.org_id == org_id,
        OntologyObject.is_deleted == False).first()  # noqa: E712


def get_object_by_name(db: Session, org_id: str, name: str) -> OntologyObject | None:
    return db.query(OntologyObject).filter(
        OntologyObject.name == name, OntologyObject.org_id == org_id,
        OntologyObject.is_deleted == False).first()  # noqa: E712


def get_action(db: Session, org_id: str, action_id: str) -> OntologyAction | None:
    return db.query(OntologyAction).filter(
        OntologyAction.id == action_id, OntologyAction.org_id == org_id,
        OntologyAction.is_deleted == False).first()  # noqa: E712
