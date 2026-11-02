"""Phase 7C T10 — retired (soft-deleted) links never surface in reads."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.ontology import OntologyDomain, OntologyLink, OntologyObject
from app.services.ontology.queries import list_links

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        OntologyDomain.__table__, OntologyObject.__table__,
        OntologyLink.__table__])
    s = sessionmaker(bind=eng)()
    s.add(OntologyDomain(id="d1", org_id=ORG, code="sales", name="Sales"))
    s.add(OntologyObject(id="o1", org_id=ORG, domain_id="d1", name="Order"))
    s.add(OntologyObject(id="o2", org_id=ORG, domain_id="d1", name="Customer"))
    s.add(OntologyLink(id="l-live", org_id=ORG, from_object_id="o1",
                       to_object_id="o2", link_type="belongs_to"))
    s.add(OntologyLink(id="l-retired", org_id=ORG, from_object_id="o2",
                       to_object_id="o1", link_type="fulfills",
                       is_deleted=True))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def test_retired_links_are_excluded(db):
    links = list_links(db, ORG)
    ids = [l.id for l in links]
    assert "l-live" in ids and "l-retired" not in ids


def test_retired_links_excluded_under_domain_filter(db):
    links = list_links(db, ORG, domain_id="d1")
    assert all(l.id != "l-retired" for l in links)
