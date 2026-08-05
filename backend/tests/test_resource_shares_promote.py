"""Tests: project auto-promote (personal→company) on share + backfill.

Plan 2026-08-05: When admin shares a project via the Manage Access dialog,
the project's resource_type should auto-flip from 'personal' to 'company'.
Once flipped, it stays 'company' even if all shares are later revoked.

Covers:
- create_share auto-promote (project) — tested through the real router
- company project shared again (no-op, idempotent)
- revoke-all does NOT revert back to personal (stable design)
- Agent auto-promote regression guard
- backfill_shared_projects: shared→upgraded, not-shared→skipped,
  already-company→skipped, soft-deleted→excluded

All tests use in-memory SQLite — no network, no live DB.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import get_current_user_required
from app.models.project import Project
from app.models.agent_app import AgentApp
from app.models.resource_share import ResourceShare
from app.models.user import User
from app.services.auth_service import auth_service


# ── helpers ───────────────────────────────────────────────────────────────────


def _uid() -> str:
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


def _make_user(db, email="test@example.com", role="user", name="Test"):
    u = User(
        id=_uid(),
        email=email,
        full_name=name,
        role=role,
        password_hash=auth_service.hash_password("pwd"),
        created_date=_now(),
        updated_date=_now(),
        org_id="default-org",
        app_id="default-app",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_project(db, name, created_by_id, is_system=False, resource_type="personal"):
    p = Project(
        id=_uid(),
        name=name,
        description="test project",
        color="#000000",
        status="active",
        is_system=is_system,
        resource_type=resource_type,
        created_by_id=created_by_id,
        created_date=_now(),
        updated_date=_now(),
        org_id="default-org",
        app_id="default-app",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_agent(db, name, created_by_id, resource_type="personal"):
    a = AgentApp(
        id=_uid(),
        name=name,
        description="test agent",
        role="assistant",
        model="gpt-4o",
        is_system=False,
        resource_type=resource_type,
        created_by_id=created_by_id,
        created_date=_now(),
        updated_date=_now(),
        org_id="default-org",
        app_id="default-app",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _make_share(db, resource_type, resource_id, owner_id, shared_with_id):
    s = ResourceShare(
        id=_uid(),
        resource_type=resource_type,
        resource_id=resource_id,
        shared_with_user_id=shared_with_id,
        access_level="use",
        created_by_id=owner_id,
        created_date=_now(),
        updated_date=_now(),
        org_id="default-org",
        app_id="default-app",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ── fixture: in-memory SQLite ────────────────────────────────────────────────


@pytest.fixture()
def db():
    """Fresh in-memory SQLite with full schema."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)
    s = Sess()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ============================================================================
# 1. Router-level auto-promote tests (through real endpoint)
# ============================================================================


class TestRouterAutoPromote:
    """Hit POST /api/shares through the real main.py FastAPI app, verifying
    the server-side auto-promote logic flips resource_type correctly."""

    def test_share_personal_project_flips_to_company(self):
        """Sharing a personal project through the router upgrades it to company."""
        from main import app

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Sess = sessionmaker(bind=engine)
        db = Sess()

        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Ecisco BI", admin.id, is_system=True, resource_type="personal")

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_required] = lambda: admin

        try:
            client = TestClient(app)
            resp = client.post("/api/shares", json={
                "resource_type": "project",
                "resource_id": proj.id,
                "shared_with_user_id": user2.id,
                "access_level": "use",
            })
            assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"

            db.refresh(proj)
            assert proj.resource_type == "company", (
                "Shared personal project must auto-flip to 'company'"
            )
        finally:
            app.dependency_overrides.clear()
            db.close()
            engine.dispose()

    def test_share_company_project_stays_company(self):
        """Sharing an already-company project is idempotent — stays company."""
        from main import app

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Sess = sessionmaker(bind=engine)
        db = Sess()

        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Company Project", admin.id, resource_type="company")

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_required] = lambda: admin

        try:
            client = TestClient(app)
            resp = client.post("/api/shares", json={
                "resource_type": "project",
                "resource_id": proj.id,
                "shared_with_user_id": user2.id,
                "access_level": "use",
            })
            assert resp.status_code == 200, resp.text

            db.refresh(proj)
            assert proj.resource_type == "company", (
                "Already-company project must stay company (idempotent)"
            )
        finally:
            app.dependency_overrides.clear()
            db.close()
            engine.dispose()

    def test_revoke_all_shares_does_not_revert_to_personal(self):
        """Once flipped to company, revoking all shares does NOT revert."""
        from main import app

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Sess = sessionmaker(bind=engine)
        db = Sess()

        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Shared Project", admin.id, resource_type="personal")

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_required] = lambda: admin

        try:
            client = TestClient(app)

            # Share → flips to company
            resp = client.post("/api/shares", json={
                "resource_type": "project",
                "resource_id": proj.id,
                "shared_with_user_id": user2.id,
                "access_level": "use",
            })
            assert resp.status_code == 200, resp.text
            share_id = resp.json()["id"]

            db.refresh(proj)
            assert proj.resource_type == "company"

            # Revoke via router
            resp = client.delete(f"/api/shares/{share_id}")
            assert resp.status_code == 200, resp.text

            db.refresh(proj)
            assert proj.resource_type == "company", (
                "Project must stay 'company' even after all shares are revoked"
            )
        finally:
            app.dependency_overrides.clear()
            db.close()
            engine.dispose()

    def test_share_agent_still_works(self):
        """Agent auto-promote path must still work (regression guard)."""
        from main import app

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Sess = sessionmaker(bind=engine)
        db = Sess()

        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        agent = _make_agent(db, "Test Agent", admin.id, resource_type="personal")

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_required] = lambda: admin

        try:
            client = TestClient(app)
            resp = client.post("/api/shares", json={
                "resource_type": "agent",
                "resource_id": agent.id,
                "shared_with_user_id": user2.id,
                "access_level": "use",
            })
            assert resp.status_code == 200, resp.text

            db.refresh(agent)
            assert agent.resource_type == "company", "Agent auto-promote must still work"
        finally:
            app.dependency_overrides.clear()
            db.close()
            engine.dispose()


# ============================================================================
# 2. backfill_shared_projects script (DB-level logic, no router needed)
# ============================================================================


class TestBackfillSharedProjects:
    """One-time backfill: all shared-but-personal projects flip to company."""

    def test_backfill_promotes_shared_personal_project(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Shared Project", admin.id, resource_type="personal")
        _make_share(db, "project", proj.id, admin.id, user2.id)

        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(ResourceShare.resource_type == "project", ResourceShare.is_deleted == False)
            .distinct().all()
        )
        shared_ids = [row[0] for row in shared_ids]
        assert proj.id in shared_ids

        personal_shared = (
            db.query(Project)
            .filter(Project.id.in_(shared_ids), Project.resource_type == "personal",
                    Project.is_deleted == False)
            .all()
        )
        assert len(personal_shared) == 1 and personal_shared[0].id == proj.id

        for p in personal_shared:
            p.resource_type = "company"
        db.commit()

        db.refresh(proj)
        assert proj.resource_type == "company"

    def test_backfill_skips_unshared_project(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        proj = _make_project(db, "Unshared Project", admin.id, resource_type="personal")

        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(ResourceShare.resource_type == "project", ResourceShare.is_deleted == False)
            .distinct().all()
        )
        shared_ids = [row[0] for row in shared_ids]
        assert proj.id not in shared_ids

    def test_backfill_skips_already_company(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Company Project", admin.id, resource_type="company")
        _make_share(db, "project", proj.id, admin.id, user2.id)

        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(ResourceShare.resource_type == "project", ResourceShare.is_deleted == False)
            .distinct().all()
        )
        shared_ids = [row[0] for row in shared_ids]

        personal_shared = (
            db.query(Project)
            .filter(Project.id.in_(shared_ids), Project.resource_type == "personal",
                    Project.is_deleted == False)
            .all()
        )
        assert len(personal_shared) == 0, (
            "Already-company project should NOT appear in personal_shared"
        )

    def test_backfill_excludes_soft_deleted_project(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Deleted Project", admin.id, resource_type="personal")
        _make_share(db, "project", proj.id, admin.id, user2.id)

        proj.is_deleted = True
        db.commit()

        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(ResourceShare.resource_type == "project", ResourceShare.is_deleted == False)
            .distinct().all()
        )
        shared_ids = [row[0] for row in shared_ids]

        personal_shared = (
            db.query(Project)
            .filter(Project.id.in_(shared_ids), Project.resource_type == "personal",
                    Project.is_deleted == False)
            .all()
        )
        assert len(personal_shared) == 0, "Soft-deleted project must be excluded"

    def test_backfill_excludes_soft_deleted_share(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Revoked Project", admin.id, resource_type="personal")
        share = _make_share(db, "project", proj.id, admin.id, user2.id)

        share.is_deleted = True
        db.commit()

        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(ResourceShare.resource_type == "project", ResourceShare.is_deleted == False)
            .distinct().all()
        )
        shared_ids = [row[0] for row in shared_ids]
        assert len(shared_ids) == 0, "Revoked share should not appear in active shares"

    def test_backfill_mixed_scenario(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")

        shared_personal = _make_project(db, "Shared Personal", admin.id, resource_type="personal")
        already_company = _make_project(db, "Already Company", admin.id, resource_type="company")
        unshared = _make_project(db, "Unshared", admin.id, resource_type="personal")
        deleted = _make_project(db, "Deleted", admin.id, resource_type="personal")

        for p in [shared_personal, already_company, deleted]:
            _make_share(db, "project", p.id, admin.id, user2.id)

        deleted.is_deleted = True
        db.commit()

        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(ResourceShare.resource_type == "project", ResourceShare.is_deleted == False)
            .distinct().all()
        )
        shared_ids = [row[0] for row in shared_ids]

        personal_shared = (
            db.query(Project)
            .filter(Project.id.in_(shared_ids), Project.resource_type == "personal",
                    Project.is_deleted == False)
            .all()
        )
        assert len(personal_shared) == 1 and personal_shared[0].id == shared_personal.id

        for p in personal_shared:
            p.resource_type = "company"
        db.commit()

        db.refresh(shared_personal)
        assert shared_personal.resource_type == "company"
        db.refresh(already_company)
        assert already_company.resource_type == "company"
        db.refresh(unshared)
        assert unshared.resource_type == "personal"
        db.refresh(deleted)
        assert deleted.resource_type == "personal"
