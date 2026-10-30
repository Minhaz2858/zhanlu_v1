"""T6 — crosswalk runtime integration: objects with confirmed identity
crosswalks expose an honest `cross_sources` summary on grounded reads.
No per-row joins in v1; never fabricates."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import IdentityMatch

ORG = "org-x"
OTHER = "org-y"

SRC_A = {"kind": "file", "kb_id": "kb-a", "sheet": "CRM"}
SRC_B = {"kind": "db", "kb_id": "kb-b", "table": "customers"}


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


def _seed(db, org, object_id, status):
    from app.services.identity.resolver import _record_match

    _record_match(db, org, object_id, SRC_A, SRC_B, "1001", "C1001",
                  "exact" if status == "confirmed" else "semantic",
                  status=status, confidence=1.0 if status == "confirmed" else 0.9)
    _record_match(db, org, object_id, SRC_A, SRC_B, "1002", "C1002",
                  "exact" if status == "confirmed" else "semantic",
                  status=status, confidence=1.0 if status == "confirmed" else 0.9)
    db.commit()


def test_crosswalk_summary_only_confirmed_and_org_scoped(db):
    from app.services.identity.review import crosswalk_summary

    _seed(db, ORG, "obj-1", "confirmed")
    _seed(db, ORG, "obj-1", "inferred")     # review queue — not active
    _seed(db, ORG, "obj-2", "confirmed")    # different object
    _seed(db, OTHER, "obj-1", "confirmed")  # different org

    s = crosswalk_summary(db, ORG, "obj-1")
    assert s["object_id"] == "obj-1"
    assert len(s["sources"]) == 1
    src = s["sources"][0]
    assert src["confirmed_pairs"] == 2
    assert src["source_a"]["kb_id"] == "kb-a"
    assert src["source_b"]["table"] == "customers"
    assert len(src["sample"]) == 2
    assert {"pk_a": "1001", "pk_b": "C1001"} in src["sample"]


def test_crosswalk_summary_empty_when_nothing_confirmed(db):
    from app.services.identity.review import crosswalk_summary

    _seed(db, ORG, "obj-1", "inferred")
    s = crosswalk_summary(db, ORG, "obj-1")
    assert s["sources"] == []


def test_read_object_data_surfaces_cross_sources(db):
    from app.models.ontology import OntologyObject
    from app.services.ontology.data import read_object_data

    db.execute(text("CREATE TABLE customers (id TEXT, name TEXT)"))
    db.execute(text("INSERT INTO customers VALUES ('C1001', 'Acme')"))
    obj = OntologyObject(
        org_id=ORG, domain_id="d1", name="Customer",
        backing_kind="table", backing_ref="customers",
        properties_json=[{"name": "id", "column": "id"},
                         {"name": "name", "column": "name"}],
    )
    db.add(obj)
    db.commit()
    _seed(db, ORG, obj.id, "confirmed")

    res = read_object_data(db, ORG, obj, limit=10)
    assert res["success"] is True
    assert res["cross_sources"]["sources"][0]["confirmed_pairs"] == 2
    assert res["cross_sources"]["sources"][0]["sample"][0]["pk_b"] == "C1001"


def test_read_object_data_without_crosswalks_has_no_block(db):
    from app.models.ontology import OntologyObject
    from app.services.ontology.data import read_object_data

    db.execute(text("CREATE TABLE vendors (id TEXT, name TEXT)"))
    obj = OntologyObject(
        org_id=ORG, domain_id="d1", name="Vendor",
        backing_kind="table", backing_ref="vendors",
        properties_json=[{"name": "id", "column": "id"}],
    )
    db.add(obj)
    db.commit()

    res = read_object_data(db, ORG, obj, limit=10)
    assert res["success"] is True
    assert "cross_sources" not in res
