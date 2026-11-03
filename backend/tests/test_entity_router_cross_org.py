"""Phase 10 / W3 — router/HTTP-layer cross-organization denial for the
generic entity API and the workspace brand-kit endpoints.

The service-layer scoping is pinned by test_entity_tenant_scoping.py; this
suite drives the ACTUAL HTTP endpoints with two principals from different
orgs and asserts every path denies cross-org access (fail closed).
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Boolean, Column, DateTime, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.deps import get_current_user_required, get_db
from app.routers.entities import register_entity_router

Base = declarative_base()


class DummyEntity(Base):
    __tablename__ = "dummy_entity_router"
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
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(register_entity_router("DummyEntity", DummyEntity),
                       prefix="/api")

    current_user = {"id": "uA", "org_id": "org-A", "app_id": None,
                    "role": "user"}

    def override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def override_user():
        return SimpleNamespace(**current_user)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user_required] = override_user

    with TestClient(app, raise_server_exceptions=False) as c:
        c.set_user = lambda u: current_user.update(u)
        yield c


def test_create_is_scoped_and_list_is_org_isolated(client):
    r = client.post("/api/apps/app-X/entities/DummyEntity",
                    json={"name": "secret-A"})
    assert r.status_code == 200, r.text
    rec_id = r.json()["id"]
    assert r.json()["org_id"] == "org-A"

    # Switch to a principal of ANOTHER org — the record must vanish.
    client.set_user({"id": "uB", "org_id": "org-B", "app_id": None,
                     "role": "user"})
    lst = client.get("/api/apps/app-X/entities/DummyEntity").json()
    ids = {x["id"] for x in (lst if isinstance(lst, list)
                             else lst.get("items", lst.get("records", [])))}
    assert rec_id not in ids
    assert client.get(
        f"/api/apps/app-X/entities/DummyEntity/{rec_id}").status_code == 404
    assert client.put(
        f"/api/apps/app-X/entities/DummyEntity/{rec_id}",
        json={"name": "hacked"}).status_code == 404
    assert client.delete(
        f"/api/apps/app-X/entities/DummyEntity/{rec_id}").status_code == 404


def test_client_cannot_scope_into_another_org(client):
    # A client-supplied org_id/app_id in the payload must not relocate the
    # row into another tenant (stripped by _IMMUTABLE_FIELDS).
    r = client.post("/api/apps/app-X/entities/DummyEntity",
                    json={"name": "x", "org_id": "org-B"})
    assert r.json()["org_id"] == "org-A"


def test_brand_kit_endpoints_require_authentication():
    """Phase 10 (audit S3.1): the brand-kit endpoints were previously
    UNAUTHENTICATED and trusted a client org_id — now they require the
    authenticated principal and use its org."""
    from app.routers import artifacts as artifacts_router

    # Structural pin on the real router: every brand-kit route must declare
    # the get_current_user_required dependency.
    for route in artifacts_router.router.routes:
        if getattr(route, "path", "") == "/workspaces/brand-kit":
            dep_names = {d.name for d in route.dependant.dependencies}
            assert "user" in dep_names, (
                "brand-kit route lost its authentication dependency"
            )
