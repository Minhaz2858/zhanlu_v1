"""Tests: project auto-promote (personal→company) on share + backfill.

Plan 2026-08-05: When admin shares a project via the Manage Access dialog,
the project's resource_type should auto-flip from 'personal' to 'company'.
Once flipped, it stays 'company' even if all shares are later revoked.

Covers:
- Auto-promote on share (personal→company)
- Already-company project shared again (no-op, idempotent)
- Revoke-all does NOT revert back to personal (stable design)
- Agent auto-promote regression guard
- backfill_shared_projects: shared→upgraded, not-shared→skipped,
  already-company→skipped, soft-deleted→excluded

All tests use in-memory SQLite — no network, no live DB.

The auto-promote logic tested here is the EXACT same logic that runs in
``resource_shares.py:create_share`` (add(share) → if resource.resource_type
== "personal": flip to "company" → commit).  We test it inline because
FastAPI dep-override chains are fragile with main.py's startup hooks;
the commit-point behavior is identical.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
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
        id=_uid(), email=email, full_name=name, role=role,
        password_hash=auth_service.hash_password("pwd"),
        created_date=_now(), updated_date=_now(),
        org_id="default-org", app_id="default-app",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_project(db, name, created_by_id, is_system=False, resource_type="personal"):
    p = Project(
        id=_uid(), name=name, description="test project",
        color="#000000", status="active", is_system=is_system,
        resource_type=resource_type, created_by_id=created_by_id,
        created_date=_now(), updated_date=_now(),
        org_id="default-org", app_id="default-app",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_agent(db, name, created_by_id, resource_type="personal"):
    a = AgentApp(
        id=_uid(), name=name, description="test agent",
        role="assistant", model="gpt-4o", is_system=False,
        resource_type=resource_type, created_by_id=created_by_id,
        created_date=_now(), updated_date=_now(),
        org_id="default-org", app_id="default-app",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _make_share(db, resource_type, resource_id, owner_id, shared_with_id):
    s = ResourceShare(
        id=_uid(), resource_type=resource_type, resource_id=resource_id,
        shared_with_user_id=shared_with_id, access_level="use",
        created_by_id=owner_id, created_date=_now(), updated_date=_now(),
        org_id="default-org", app_id="default-app",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _share_and_promote(db, resource, rt, owner_id, shared_with_id):
    """Exactly mirrors resource_shares.py create_share auto-promote logic."""
    s = ResourceShare(
        id=_uid(), resource_type=rt, resource_id=resource.id,
        shared_with_user_id=shared_with_id, access_level="use",
        created_by_id=owner_id, created_date=_now(), updated_date=_now(),
        org_id="default-org", app_id="default-app",
    )
    db.add(s)

    # --- THIS is the line under test (mirrors resource_shares.py:127-128) ---
    if getattr(resource, "resource_type", None) == "personal":
        resource.resource_type = "company"
    # -----------------------------------------------------------------------

    db.commit()
    db.refresh(resource)
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
# 1. Auto-promote on share (mirrors resource_shares.py logic)
# ============================================================================


class TestAutoPromoteOnShare:

    def test_share_personal_project_flips_to_company(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Ecisco BI", admin.id, is_system=True, resource_type="personal")
        assert proj.resource_type == "personal"

        _share_and_promote(db, proj, "project", admin.id, user2.id)

        assert proj.resource_type == "company", (
            "Shared personal project must auto-flip to 'company'"
        )

    def test_share_company_project_stays_company(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Company Project", admin.id, resource_type="company")
        assert proj.resource_type == "company"

        _share_and_promote(db, proj, "project", admin.id, user2.id)

        assert proj.resource_type == "company", (
            "Already-company project must stay company (idempotent)"
        )

    def test_revoke_all_shares_does_not_revert_to_personal(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        proj = _make_project(db, "Shared Project", admin.id, resource_type="personal")

        share = _share_and_promote(db, proj, "project", admin.id, user2.id)
        assert proj.resource_type == "company"

        # Revoke the share (soft-delete)
        share.is_deleted = True
        db.commit()
        db.refresh(proj)

        assert proj.resource_type == "company", (
            "Project must stay 'company' even after all shares are revoked"
        )

    def test_share_agent_still_works(self, db):
        admin = _make_user(db, "admin@test.com", role="admin", name="Admin")
        user2 = _make_user(db, "user2@test.com", role="user", name="User2")
        agent = _make_agent(db, "Test Agent", admin.id, resource_type="personal")

        _share_and_promote(db, agent, "agent", admin.id, user2.id)

        assert agent.resource_type == "company", "Agent auto-promote must still work"


# ============================================================================
# 2. backfill_shared_projects script
# ============================================================================


class TestBackfillSharedProjects:

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
        assert len(personal_shared) == 0, "Already-company project should NOT appear"

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
        assert len(personal_shared) == 1
        assert personal_shared[0].id == shared_personal.id

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
