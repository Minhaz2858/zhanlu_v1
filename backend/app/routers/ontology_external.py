"""Ontology external surface (P2-1, Palantir gap G12).

Machine-to-machine, READ-ONLY: third-party tools and agent platforms can
ask "what objects/actions/links does this org expose?" without a human
login. Auth = shared secret ``X-API-Key`` matching ``ONTOLOGY_API_KEY``
(same pattern as A2A/ACP) — empty setting = surface disabled.

What is deliberately NOT here: no writes, no raw data rows (objects
describe the semantic layer; data stays behind the grounded-read paths
with their org-ownership checks), no cross-org listing (caller names one
org; scoping is enforced by the same _org rules as the interactive API).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ontology-external"])


def _require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    expected = getattr(settings, "ONTOLOGY_API_KEY", "") or ""
    if not expected or not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def _load_org(db: Session, org_id: str) -> None:
    from app.models.organization import Organization

    if not db.query(Organization).filter(
            Organization.id == org_id).first():
        raise HTTPException(status_code=404, detail="Organization not found")


@router.get("/integrations/ontology/{org_id}/objects",
            dependencies=[Depends(_require_api_key)])
def external_objects(org_id: str, db: Session = Depends(get_db)):
    _load_org(db, org_id)
    from app.services.ontology.queries import list_objects
    from app.services.ontology.serialize import object_to_dict

    return [object_to_dict(o) for o in list_objects(db, org_id)]


@router.get("/integrations/ontology/{org_id}/actions",
            dependencies=[Depends(_require_api_key)])
def external_actions(org_id: str, db: Session = Depends(get_db)):
    _load_org(db, org_id)
    from app.services.ontology.queries import list_actions
    from app.services.ontology.serialize import action_to_dict

    return [action_to_dict(a) for a in list_actions(db, org_id)]


@router.get("/integrations/ontology/{org_id}/links",
            dependencies=[Depends(_require_api_key)])
def external_links(org_id: str, db: Session = Depends(get_db)):
    _load_org(db, org_id)
    from app.services.ontology.queries import list_links
    from app.services.ontology.serialize import link_to_dict

    return [link_to_dict(l) for l in list_links(db, org_id)]


@router.get("/integrations/ontology/{org_id}/directory",
            dependencies=[Depends(_require_api_key)])
def external_directory(
    org_id: str,
    domain: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """One-call discovery: domains → objects → actions → links. The
    "what can your agents do here" answer for an external platform."""
    _load_org(db, org_id)
    from app.services.ontology.queries import (
        list_actions,
        list_domains,
        list_links,
        list_objects,
    )
    from app.services.ontology.serialize import (
        action_to_dict,
        domain_to_dict,
        link_to_dict,
        object_to_dict,
    )

    domains = list_domains(db, org_id)
    if domain:
        domains = [d for d in domains if d.code == domain]
    objects = list_objects(db, org_id)
    actions = list_actions(db, org_id)
    links = list_links(db, org_id)
    return {
        "org_id": org_id,
        "domains": [
            {
                **domain_to_dict(d),
                "objects": [object_to_dict(o) for o in objects
                            if o.domain_id == d.id],
                "actions": [action_to_dict(a) for a in actions
                            if any(o.id == a.object_id and o.domain_id == d.id
                                   for o in objects)],
            }
            for d in domains
        ],
        "links": [link_to_dict(l) for l in links],
    }
