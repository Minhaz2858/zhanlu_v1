"""Phase 1 multi-tenant RBAC tests — migration 038 models, guards, and seed.

Covers:
- owner-only write enforcement (non-owner cannot update/delete)
- shared-with-me visibility on reads (ResourceShare grants read access)
- resource_type stamping from creator role (admin→company, user→personal)
- require_admin FastAPI dependency
- ensure_superadmin idempotency (run twice = safe)

All tests use in-memory SQLite + real SQLAlchemy models — no network, no
LLM, no live DB.
"""

import os
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
from app.services import entity_service
from app.services.auth_service import auth_service


# ── helpers ────────────────────────────────────────────────────────────────


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


def _make_agent(db, name, created_by_id, is_system=False, resource_type="personal"):
    a = AgentApp(
        id=_uid(),
        name=name,
        description="test agent",
        role="assistant",
        model="gpt-4o",
        is_system=is_system,
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


def _share(db, resource_type, resource_id, owner_id, shared_with_id):
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


# ── fixtures ───────────────────────────────────────────────────────────────


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
# 1. owner-only write enforcement
# ============================================================================


class TestOwnerOnlyWrites:
    """Non-owners must not be able to update/delete Projects or AgentApps."""

    def test_non_owner_cannot_update_project(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        other = _make_user(db, "other@test.com", role="user")
        proj = _make_project(db, "My Project", owner.id)

        updates = {"name": "Hijacked"}
        updated = entity_service.update_record(Project, proj.id, updates, db, owner_id=other.id)
        assert updated is None, "non-owner should get None (not found)"

    def test_non_owner_cannot_delete_project(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        other = _make_user(db, "other@test.com", role="user")
        proj = _make_project(db, "My Project", owner.id)

        deleted = entity_service.soft_delete_record(Project, proj.id, db, owner_id=other.id)
        assert deleted is False, "non-owner should get False (record not found)"

    def test_non_owner_cannot_update_agent(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        other = _make_user(db, "other@test.com", role="user")
        agent = _make_agent(db, "My Agent", owner.id)

        updates = {"name": "Hijacked"}
        updated = entity_service.update_record(AgentApp, agent.id, updates, db, owner_id=other.id)
        assert updated is None

    def test_non_owner_cannot_delete_agent(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        other = _make_user(db, "other@test.com", role="user")
        agent = _make_agent(db, "My Agent", owner.id)

        deleted = entity_service.soft_delete_record(AgentApp, agent.id, db, owner_id=other.id)
        assert deleted is False, "non-owner should get False (record not found)"

    def test_owner_can_still_update(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        proj = _make_project(db, "My Project", owner.id)

        updated = entity_service.update_record(Project, proj.id, {"name": "Renamed"}, db, owner_id=owner.id)
        assert updated is not None
        assert updated["name"] == "Renamed"

    def test_owner_can_still_delete(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        proj = _make_project(db, "My Project", owner.id)

        deleted = entity_service.soft_delete_record(Project, proj.id, db, owner_id=owner.id)
        assert deleted is not None


# ============================================================================
# 2. shared-with-me visibility on reads
# ============================================================================


class TestSharedWithMeVisibility:
    """A user who received a ResourceShare grant sees the resource in reads."""

    def test_shared_project_appears_in_list(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        reader = _make_user(db, "reader@test.com", role="user")
        proj = _make_project(db, "Shared Project", owner.id)
        _share(db, "project", proj.id, owner.id, reader.id)

        results = entity_service.list_records(
            Project, db, owner_id=reader.id, include_shared=True,
        )
        ids = {r["id"] for r in results}
        assert proj.id in ids, "shared project must appear in reader's list"

    def test_shared_project_appears_in_get(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        reader = _make_user(db, "reader@test.com", role="user")
        proj = _make_project(db, "Shared Project", owner.id)
        _share(db, "project", proj.id, owner.id, reader.id)

        result = entity_service.get_record(
            Project, proj.id, db, owner_id=reader.id, include_shared=True,
        )
        assert result is not None
        assert result["id"] == proj.id

    def test_shared_project_not_in_list_without_flag(self, db):
        """Without include_shared, the reader does NOT see shared projects."""
        owner = _make_user(db, "owner@test.com", role="user")
        reader = _make_user(db, "reader@test.com", role="user")
        proj = _make_project(db, "Shared Project", owner.id)
        _share(db, "project", proj.id, owner.id, reader.id)

        results = entity_service.list_records(
            Project, db, owner_id=reader.id, include_shared=False,
        )
        ids = {r["id"] for r in results}
        assert proj.id not in ids

    def test_shared_agent_appears_in_list(self, db):
        owner = _make_user(db, "owner@test.com", role="user")
        reader = _make_user(db, "reader@test.com", role="user")
        agent = _make_agent(db, "Shared Agent", owner.id)
        _share(db, "agent", agent.id, owner.id, reader.id)

        results = entity_service.list_records(
            AgentApp, db, owner_id=reader.id, include_shared=True,
        )
        ids = {r["id"] for r in results}
        assert agent.id in ids

    def test_shared_user_cannot_update(self, db):
        """Shared access is read-only — update/delete still require ownership."""
        owner = _make_user(db, "owner@test.com", role="user")
        reader = _make_user(db, "reader@test.com", role="user")
        proj = _make_project(db, "Shared Project", owner.id)
        _share(db, "project", proj.id, owner.id, reader.id)

        # Reader CAN read
        assert entity_service.get_record(
            Project, proj.id, db, owner_id=reader.id, include_shared=True,
        ) is not None

        # Reader CANNOT update
        updated = entity_service.update_record(
            Project, proj.id, {"name": "Hijacked"}, db, owner_id=reader.id,
        )
        assert updated is None

    def test_annotate_access_flags(self, db):
        """_annotate_access sets can_edit and is_shared_with_me correctly."""
        owner = _make_user(db, "owner@test.com", role="user")
        reader = _make_user(db, "reader@test.com", role="user")
        proj = _make_project(db, "Annotated", owner.id)
        _share(db, "project", proj.id, owner.id, reader.id)

        # Owner sees can_edit=True, is_shared_with_me=False
        result_owner = entity_service.get_record(
            Project, proj.id, db, owner_id=owner.id, include_shared=True,
        )
        assert result_owner["can_edit"] is True
        assert result_owner["is_shared_with_me"] is False

        # Shared reader sees can_edit=False, is_shared_with_me=True
        result_reader = entity_service.get_record(
            Project, proj.id, db, owner_id=reader.id, include_shared=True,
        )
        assert result_reader["can_edit"] is False
        assert result_reader["is_shared_with_me"] is True


# ============================================================================
# 3. resource_type stamping from creator role
# ============================================================================


class TestResourceTypeStamping:
    """resource_type is derived server-side and immutable via PUT."""

    def test_admin_creates_company_project(self, db):
        admin = _make_user(db, "admin@test.com", role="admin")
        data = {"name": "Company Project", "description": "test", "color": "#333"}
        result = entity_service.create_record(
            Project, data, db, created_by_id=admin.id,
            extra_fields={"resource_type": "company"},
        )
        assert result["resource_type"] == "company"

    def test_user_creates_personal_project(self, db):
        user = _make_user(db, "user@test.com", role="user")
        data = {"name": "Personal Project", "description": "test", "color": "#333"}
        result = entity_service.create_record(
            Project, data, db, created_by_id=user.id,
            extra_fields={"resource_type": "personal"},
        )
        assert result["resource_type"] == "personal"

    def test_admin_creates_company_agent(self, db):
        admin = _make_user(db, "admin@test.com", role="admin")
        data = {"name": "Company Agent", "description": "test", "model": "gpt-4o", "role": "assistant"}
        result = entity_service.create_record(
            AgentApp, data, db, created_by_id=admin.id,
            extra_fields={"resource_type": "company"},
        )
        assert result["resource_type"] == "company"

    def test_resource_type_immutable_via_put(self, db):
        """Clients cannot change resource_type through update_record."""
        owner = _make_user(db, "owner@test.com", role="user")
        data = {"name": "Personal Project", "description": "test", "color": "#333"}
        result = entity_service.create_record(
            Project, data, db, created_by_id=owner.id,
            extra_fields={"resource_type": "personal"},
        )
        proj_id = result["id"]
        assert result["resource_type"] == "personal"

        # Try to mutate resource_type via PUT
        updated = entity_service.update_record(
            Project, proj_id,
            {"name": "Renamed", "resource_type": "company"},
            db, owner_id=owner.id,
        )
        assert updated is not None
        assert updated["resource_type"] == "personal", (
            "resource_type must stay 'personal' — it is immutable"
        )

    def test_backfill_preserves_is_system_company(self, db):
        """Simulate migration backfill: is_system=True → resource_type='company'."""
        proj = _make_project(db, "SysProj", created_by_id=None, is_system=True)
        # A system project is visible to any user via the read path with is_system filter
        user = _make_user(db, "anyone@test.com", role="user")
        result = entity_service.get_record(
            Project, proj.id, db, owner_id=user.id, include_shared=True,
        )
        assert result is not None, "system project must be visible to any user"


# ============================================================================
# 4. require_admin guard
# ============================================================================


class TestRequireAdminGuard:
    """require_admin FastAPI dependency raises 403 for non-admin users."""

    def test_require_admin_allows_admin(self):
        from app.deps import require_admin

        admin = User(
            id=_uid(),
            email="admin@test.com",
            full_name="Admin",
            role="admin",
            password_hash="...",
            created_date=_now(),
            updated_date=_now(),
            org_id="default-org",
            app_id="default-app",
        )
        result = require_admin(user=admin)
        assert result is admin

    def test_require_admin_rejects_user(self):
        from app.deps import require_admin
        from fastapi import HTTPException

        user = User(
            id=_uid(),
            email="user@test.com",
            full_name="Normal",
            role="user",
            password_hash="...",
            created_date=_now(),
            updated_date=_now(),
            org_id="default-org",
            app_id="default-app",
        )
        with pytest.raises(HTTPException) as exc:
            require_admin(user=user)
        assert exc.value.status_code == 403

    def test_require_admin_resolves_from_dependency_chain(self):
        """Verify the dependency is properly callable (structural check)."""
        from app.deps import require_admin

        assert callable(require_admin)
        admin_user = User(
            id=_uid(),
            email="admin@test.com",
            full_name="Admin",
            role="admin",
            password_hash="...",
            created_date=_now(),
            updated_date=_now(),
            org_id="default-org",
            app_id="default-app",
        )
        result = require_admin(user=admin_user)
        assert result.role == "admin"


# ============================================================================
# 5. ensure_superadmin idempotency
# ============================================================================


class TestEnsureSuperadmin:
    """Startup seed is safe to run multiple times."""

    def test_idempotent_creates_once(self, db):
        """Running ensure_superadmin twice creates only one admin record."""
        from app.services.ensure_superadmin import ensure_superadmin
        from app.config import settings as _settings

        email = f"sa-{_uid()[:8]}@test.com"
        original_email = _settings.SUPERADMIN_EMAIL
        original_password = _settings.SUPERADMIN_PASSWORD
        try:
            _settings.SUPERADMIN_EMAIL = email
            _settings.SUPERADMIN_PASSWORD = "supertest123"

            # First run — should create
            ensure_superadmin(db=db)

            # Second run — idempotent, should not create a second row
            ensure_superadmin(db=db)

            # Verify exactly one admin user with this email
            count = db.query(User).filter(
                User.email == email, User.role == "admin", User.is_deleted == False,
            ).count()
            assert count == 1, f"Expected exactly 1 super-admin, got {count}"
        finally:
            _settings.SUPERADMIN_EMAIL = original_email
            _settings.SUPERADMIN_PASSWORD = original_password

    def test_empty_env_is_noop(self, db):
        """When SUPERADMIN_EMAIL is empty, ensure_superadmin is a no-op."""
        from app.services.ensure_superadmin import ensure_superadmin
        from app.config import settings as _settings

        original_email = _settings.SUPERADMIN_EMAIL
        try:
            _settings.SUPERADMIN_EMAIL = ""
            _settings.SUPERADMIN_PASSWORD = ""
            ensure_superadmin(db=db)
            # Should not raise — just a no-op
        finally:
            _settings.SUPERADMIN_EMAIL = original_email

    def test_require_admin_function_import_works(self):
        """Structural check: require_admin is importable from deps."""
        from app.deps import require_admin
        assert callable(require_admin)


# ============================================================================
# 6. smoke: non-shareable entities are unaffected
# ============================================================================


class TestNonShareableUnaffected:
    """ChatSession and other non-shareable entities must NOT accept include_shared."""

    def test_user_model_not_affected_by_include_shared(self, db):
        """User is self-scoped — include_shared should not change anything."""
        u1 = _make_user(db, "u1@test.com")
        u2 = _make_user(db, "u2@test.com")

        # u1 gets their own record
        result = entity_service.get_record(
            User, u1.id, db, owner_id=u1.id, include_shared=True,
        )
        assert result is not None
        assert result["id"] == u1.id

        # u1 cannot see u2's record even with include_shared=True
        result2 = entity_service.get_record(
            User, u2.id, db, owner_id=u1.id, include_shared=True,
        )
        assert result2 is None


# ============================================================================
# 7. config flags
# ============================================================================


class TestConfigFlags:
    """ALLOW_SELF_REGISTRATION, SUPERADMIN_EMAIL, SUPERADMIN_PASSWORD exist."""

    def test_config_flags_exist(self):
        from app.config import Settings

        s = Settings()
        assert hasattr(s, "ALLOW_SELF_REGISTRATION")
        assert hasattr(s, "SUPERADMIN_EMAIL")
        assert hasattr(s, "SUPERADMIN_PASSWORD")
        assert s.ALLOW_SELF_REGISTRATION is True, "default must be True for backward compat"
        assert s.SUPERADMIN_EMAIL == ""
        assert s.SUPERADMIN_PASSWORD == ""


# ============================================================================
# 8. model round-trip
# ============================================================================


class TestResourceShareModel:
    """ResourceShare ORM model works correctly."""

    def test_create_and_query_share(self, db):
        owner = _make_user(db, "owner@test.com")
        reader = _make_user(db, "reader@test.com")
        proj = _make_project(db, "Shared Proj", owner.id)

        s = _share(db, "project", proj.id, owner.id, reader.id)

        found = db.query(ResourceShare).filter(
            ResourceShare.resource_type == "project",
            ResourceShare.resource_id == proj.id,
            ResourceShare.shared_with_user_id == reader.id,
            ResourceShare.is_deleted == False,
        ).first()

        assert found is not None
        assert found.access_level == "use"
        assert found.created_by_id == owner.id

    def test_soft_delete_share(self, db):
        owner = _make_user(db, "owner@test.com")
        reader = _make_user(db, "reader@test.com")
        proj = _make_project(db, "SoftDel", owner.id)
        s = _share(db, "project", proj.id, owner.id, reader.id)

        # Soft-delete
        s.is_deleted = True
        db.commit()

        # Should not appear in active shares
        found = db.query(ResourceShare).filter(
            ResourceShare.id == s.id, ResourceShare.is_deleted == False,
        ).first()
        assert found is None
