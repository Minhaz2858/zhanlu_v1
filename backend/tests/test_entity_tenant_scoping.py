"""Regression tests for audit P1 — entity API tenant scoping.

The generic entity CRUD API previously hardcoded ``default-org`` /
``default-app`` in ``entity_service._tenant_filters`` and never stamped a
tenant on rows it created (``_filter_data`` strips ``org_id``/``app_id`` from
client payloads). For any non-default-org tenant that silently broke reads
and left writes unscoped — and for the 17 tables outside RLS it was the ONLY
isolation. These tests lock in that the authenticated principal's org/app is
now the scoping authority.
"""
import pytest
import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.services import entity_service

Base = declarative_base()


class _Dummy(Base):
    """Minimal multi-tenant entity used to exercise entity_service directly.

    Mirrors real models: the PK is server-generated (entity_service strips any
    client-supplied ``id``), so we give the column a default. ``created_date`` /
    ``updated_date`` exist because ``parse_sort`` defaults to them.
    """

    __tablename__ = "dummy_entity"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String)
    org_id = Column(String)
    app_id = Column(String)
    is_deleted = Column(Boolean, default=False)
    created_by_id = Column(String)
    created_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        yield s


def test_tenant_filters_use_passed_org_not_default():
    conds = entity_service._tenant_filters(_Dummy, "org-X", "app-Y")
    assert len(conds) == 2
    # org_id condition first, app_id second — and neither is the default.
    assert conds[0].right.value == "org-X"
    assert conds[1].right.value == "app-Y"


def test_tenant_filters_fallback_to_default_when_omitted():
    conds = entity_service._tenant_filters(_Dummy)
    assert conds[0].right.value == settings.DEFAULT_ORG_ID
    assert conds[1].right.value == settings.DEFAULT_APP_ID


def test_create_record_stamps_authenticated_tenant(db):
    rec = entity_service.create_record(
        _Dummy, {"name": "a"}, db, "user1", org_id="org-X", app_id="app-Y"
    )
    assert rec["org_id"] == "org-X"
    assert rec["app_id"] == "app-Y"


def test_create_record_defaults_to_platform_org_when_omitted(db):
    rec = entity_service.create_record(_Dummy, {"name": "b"}, db, "user1")
    assert rec["org_id"] == settings.DEFAULT_ORG_ID
    assert rec["app_id"] == settings.DEFAULT_APP_ID


def test_client_cannot_tamper_with_org_on_create(db):
    # A client-supplied org_id must be ignored; the server stamp wins.
    rec = entity_service.create_record(
        _Dummy,
        {"name": "c", "org_id": "evil-org", "app_id": "evil-app"},
        db,
        "user1",
        org_id="org-X",
        app_id="app-Y",
    )
    assert rec["org_id"] == "org-X"
    assert rec["app_id"] == "app-Y"


def test_list_records_scoped_to_tenant(db):
    entity_service.create_record(_Dummy, {"name": "a"}, db, "u", org_id="org-X", app_id="app-Y")
    entity_service.create_record(_Dummy, {"name": "b"}, db, "u", org_id="org-Y", app_id="app-Y")

    x_rows = entity_service.list_records(_Dummy, db, org_id="org-X", app_id="app-Y")
    assert {r["name"] for r in x_rows} == {"a"}

    y_rows = entity_service.list_records(_Dummy, db, org_id="org-Y", app_id="app-Y")
    assert {r["name"] for r in y_rows} == {"b"}


def test_list_records_default_scope_does_not_leak_other_tenants(db):
    # Default (no explicit org) must scope to default-org, never to org-X.
    entity_service.create_record(_Dummy, {"name": "x"}, db, "u", org_id="org-X", app_id="app-Y")
    entity_service.create_record(
        _Dummy, {"name": "def"}, db, "u",
        org_id=settings.DEFAULT_ORG_ID, app_id=settings.DEFAULT_APP_ID,
    )
    rows = entity_service.list_records(_Dummy, db)  # no org args
    assert {r["name"] for r in rows} == {"def"}
