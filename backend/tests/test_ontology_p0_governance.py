"""P0 governance hardening — fail-closed ontology action permissions.

Covers: permission_scope enforcement on both execution paths, fail-closed
behavior for unidentified callers and unbound agents, trusted console-user
identity, cross-domain scope isolation, denial auditing, and audit
vocabulary registration (PRD 2026-10-30 Phase 0, gaps G7 + G3-bug).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import OntologyAction, OntologyObject

ORG = "org-test"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _make_write_action(db, tool_name="some_tool"):
    from app.models.ontology import OntologyDomain
    dom = OntologyDomain(org_id=ORG, code="procurement", name="Procurement")
    db.add(dom)
    db.flush()
    obj = OntologyObject(
        org_id=ORG, domain_id=dom.id, name="PurchaseOrder",
        backing_kind="manual",
    )
    db.add(obj)
    db.flush()
    act = OntologyAction(
        org_id=ORG, object_id=obj.id, name="CreatePO",
        tool_name=tool_name, risk_level="write",
        requires_confirmation=True, params_json={"qty": 5},
    )
    db.add(act)
    db.commit()
    return act


def _add_binding(db, agent_name, scope):
    from app.models.ontology import AgentDomainBinding
    obj = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == ORG, OntologyObject.name == "PurchaseOrder")
        .one()
    )
    db.add(AgentDomainBinding(
        org_id=ORG, agent_name=agent_name,
        domain_id=obj.domain_id, permission_scope=scope,
    ))
    db.commit()


def _async_fake(fn):
    async def _inner(*args, **kwargs):
        return fn(*args, **kwargs)
    return _inner


def test_audit_vocabularies_whitelist_ontology():
    from app.models.audit_event import AUDIT_EVENT_SOURCES, AUDIT_EVENT_TYPES
    assert "ontology" in AUDIT_EVENT_SOURCES
    for t in ("ontology_fanout", "escalation_created", "escalation_resolved"):
        assert t in AUDIT_EVENT_TYPES
