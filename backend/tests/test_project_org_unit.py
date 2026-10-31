"""Phase 1B.3 — Project → OrgUnit authoritative department link.

Covers: linking via the generic entity API (create/update), legacy
`department` label back-fill, hierarchy annotation, fail-closed cross-org
/ missing / wrong-unit_type rejection, unlinked projects fully
functional, unrelated-field preservation on update, and — critically —
that a department association does NOT expand project authorization
(ownership + ResourceShare semantics unchanged).
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.enterprise_process import OrgUnit
from app.models.project import Project
from app.models.resource_share import ResourceShare
from app.models.user import User
from app.services.entity_service import (
    create_record, get_record, list_records, update_record)
from app.services.project_access import project_use_authorized

ORG = "org-acme"
ORG2 = "org-other"


def _user(uid, role="user", org=ORG):
    return User(id=uid, email=f"{uid}@t.dev", full_name=uid.title(),
                role=role, password_hash="x", org_id=org)


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Project.__table__, OrgUnit.__table__, User.__table__,
        ResourceShare.__table__,
    ])
    s = sessionmaker(bind=eng)()
    s.add(OrgUnit(id="ou-eng", org_id=ORG, name="Engineering",
                  unit_type="department"))
    s.add(OrgUnit(id="ou-mfg", org_id=ORG, name="Manufacturing",
                  unit_type="department", parent_name="Engineering"))
    s.add(OrgUnit(id="ou-role", org_id=ORG, name="Duty manager",
                  unit_type="role"))
    s.add(OrgUnit(id="ou-evil", org_id=ORG2, name="Evil dept",
                  unit_type="department"))
    s.add(Project(id="p-linked", name="Linked", org_id=ORG,
                  created_by_id="u-owner"))
    s.add(Project(id="p-plain", name="Plain", org_id=ORG,
                  created_by_id="u-owner", description="legacy project"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _owner():
    return _user("u-owner")


# 1. Project can link to an OrgUnit -----------------------------------------

def test_create_links_unit(db):
    out = create_record(Project, {
        "name": "Line optimization",
        "department_org_unit_id": "ou-mfg",
    }, db, created_by_id="u-owner", org_id=ORG, app_id="default-app")
    assert out["department_org_unit_id"] == "ou-mfg"
    assert out["department_org_unit"] == {
        "id": "ou-mfg", "name": "Manufacturing",
        "unit_type": "department",
        "hierarchy": ["Manufacturing", "Engineering"],
    }


@pytest.mark.skipif(not hasattr(Project, "department"),
                    reason="legacy department label is foreign WIP — "
                           "back-fill only applies when the column exists")
def test_create_backfills_legacy_label_from_unit(db):
    # legacy label back-filled from the authoritative name (payload wins)
    out = create_record(Project, {
        "name": "Line optimization",
        "department_org_unit_id": "ou-mfg",
    }, db, created_by_id="u-owner", org_id=ORG, app_id="default-app")
    assert out["department"] == "Manufacturing"
    # explicit label in the same payload wins over the back-fill
    out2 = create_record(Project, {
        "name": "P2", "department": "Custom label",
        "department_org_unit_id": "ou-eng",
    }, db, created_by_id="u-owner", org_id=ORG, app_id="default-app")
    assert out2["department"] == "Custom label"


def test_get_and_list_annotate_hierarchy(db):
    db.get(Project, "p-linked").department_org_unit_id = "ou-mfg"
    db.commit()
    got = get_record(Project, "p-linked", db, owner_id=None,
                           org_id=ORG, app_id="default-app")
    assert got["department_org_unit"]["hierarchy"] == \
        ["Manufacturing", "Engineering"]
    rows = list_records(Project, db, org_id=ORG, app_id="default-app")
    by_name = {r["name"]: r for r in rows}
    assert by_name["Linked"]["department_org_unit"]["name"] == \
        "Manufacturing"
    # unlinked rows carry no annotation
    assert "department_org_unit" not in by_name["Plain"]


# 2. Cross-org / missing / wrong-type rejected -------------------------------

def test_cross_org_and_invalid_assignments_rejected(db):
    with pytest.raises(HTTPException) as e1:
        update_record(Project, "p-plain", {
            "department_org_unit_id": "ou-evil"}, db,
            org_id=ORG, app_id="default-app")
    assert e1.value.status_code == 404  # cross-org indistinguishable from missing
    with pytest.raises(HTTPException) as e2:
        update_record(Project, "p-plain", {
            "department_org_unit_id": "ou-missing"}, db,
            org_id=ORG, app_id="default-app")
    assert e2.value.status_code == 404
    with pytest.raises(HTTPException) as e3:
        update_record(Project, "p-plain", {
            "department_org_unit_id": "ou-role"}, db,
            org_id=ORG, app_id="default-app")
    assert e3.value.status_code == 400  # 'role' units are not containers
    # failed attempts write nothing
    row = db.get(Project, "p-plain")
    db.refresh(row)
    assert row.department_org_unit_id is None


# 3. Existing projects without OrgUnit remain functional ---------------------

def test_unlinked_projects_functional(db):
    out = update_record(Project, "p-plain", {
        "description": "still legacy"}, db,
        org_id=ORG, app_id="default-app")
    assert out["description"] == "still legacy"
    assert out["department_org_unit_id"] is None
    assert "department_org_unit" not in out
    # explicit unlink clears the id (label, if present, is never erased)
    row = db.get(Project, "p-linked")
    row.department_org_unit_id = "ou-eng"
    if hasattr(row, "department"):
        row.department = "Engineering"
    db.commit()
    out = update_record(Project, "p-linked", {
        "department_org_unit_id": None}, db,
        org_id=ORG, app_id="default-app")
    assert out["department_org_unit_id"] is None
    if hasattr(Project, "department"):
        assert out["department"] == "Engineering"  # label preserved


# 4. Updating preserves unrelated fields --------------------------------------

def test_update_preserves_unrelated_fields(db):
    before = db.get(Project, "p-plain")
    out = update_record(Project, "p-plain", {"name": "Renamed"}, db,
                        org_id=ORG, app_id="default-app")
    assert out["name"] == "Renamed"
    db.refresh(before)
    assert before.description == "legacy project"
    assert before.created_by_id == "u-owner"
    assert before.department_org_unit_id is None


# 5. Department association does not expand permissions -----------------------

def test_department_link_does_not_expand_authorization(db):
    db.get(Project, "p-linked").department_org_unit_id = "ou-eng"
    db.commit()
    project = db.get(Project, "p-linked")
    # creator still authorized
    assert project_use_authorized(db, _user("u-owner"), project) is True
    # same-org non-owner (even an admin of ANOTHER org) — no grant from
    # the department link; only explicit ResourceShare would grant
    assert project_use_authorized(db, _user("u-colleague"), project) is False
    assert project_use_authorized(db, _user("u-evil", role="admin",
                                             org=ORG2), project) is False
    # explicit 'use' share still grants, as before
    db.add(ResourceShare(resource_type="project", resource_id="p-linked",
                         shared_with_user_id="u-colleague",
                         org_id=ORG, access_level="use"))
    db.commit()
    assert project_use_authorized(db, _user("u-colleague"), project) is True
