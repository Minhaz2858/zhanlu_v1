"""Multi-tenant auth tests (2026-10-13 design).

Covers:
  * signup-org creates an Organization + its first admin user (and is
    disabled without ALLOW_ORG_SIGNUP)
  * login exposes the tenant context (org_id + role) — the client never
    supplies it
  * access tokens carry org_id/role claims; legacy tokens still verify
  * the admin surface is ORG-SCOPED: list shows own org only, cross-org
    users are invisible (404), created users join the admin's org
  * normal users get 403 on the admin surface
"""
import pytest
from fastapi import HTTPException

from app.config import settings
from app.services.auth_service import auth_service


def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401 — register every model on Base.metadata
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/mt_auth_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_user(db, email, role="user", org_id="default-org"):
    from app.models.user import User

    u = User(
        email=email,
        full_name=email.split("@")[0],
        role=role,
        org_id=org_id,
        password_hash=auth_service.hash_password("Passw0rd!123"),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── signup-org ────────────────────────────────────────────────────────────


async def test_signup_org_disabled_by_default(tmp_path, monkeypatch):
    from app.routers.auth import signup_org

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(settings, "ALLOW_ORG_SIGNUP", False)
    with pytest.raises(HTTPException) as e:
        await signup_org("app", {"org_name": "Acme", "email": "a@acme.io",
                           "password": "Passw0rd!123"}, db)
    assert e.value.status_code == 403


async def test_signup_org_creates_org_and_admin(tmp_path, monkeypatch):
    from app.routers.auth import signup_org
    from app.models.organization import Organization
    from app.models.user import User

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(settings, "ALLOW_ORG_SIGNUP", True)
    res = await signup_org("app", {
        "org_name": "Acme Corp", "email": "owner@acme.io",
        "password": "Passw0rd!123", "full_name": "Owner",
    }, db)
    org = db.query(Organization).filter(Organization.slug == "acme-corp").first()
    assert org is not None
    user = db.query(User).filter(User.email == "owner@acme.io").first()
    assert user.role == "admin"
    assert user.org_id == org.id
    assert res["org_id"] == org.id
    assert res["role"] == "admin"
    assert res["org"]["slug"] == "acme-corp"


async def test_signup_org_rejects_duplicate_email(tmp_path, monkeypatch):
    from app.routers.auth import signup_org

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(settings, "ALLOW_ORG_SIGNUP", True)
    body = {"org_name": "Beta", "email": "dup@x.io", "password": "Passw0rd!123"}
    await signup_org("app", body, db)
    with pytest.raises(HTTPException) as e:
        await signup_org("app", dict(body, org_name="Gamma"), db)
    assert e.value.status_code == 409


# ── login exposes the tenant context ──────────────────────────────────────


async def test_login_response_carries_org_and_role(tmp_path):
    from app.routers.auth import login

    db = _fresh_db(tmp_path)
    _make_user(db, "admin@corp.io", role="admin", org_id="org-corp")
    res = await login("app", {"email": "admin@corp.io", "password": "Passw0rd!123"}, db)
    assert res["org_id"] == "org-corp"
    assert res["role"] == "admin"


async def test_access_token_carries_org_claims(tmp_path):
    db = _fresh_db(tmp_path)
    u = _make_user(db, "claims@corp.io", role="admin", org_id="org-corp")
    token = auth_service.create_access_token(u.id, db)
    claims = auth_service.verify_token_claims(token, db)
    assert claims["sub"] == u.id
    assert claims["org_id"] == "org-corp"
    assert claims["role"] == "admin"


def test_legacy_token_without_claims_still_verifies(tmp_path):
    """Backward compatibility: pre-upgrade tokens authenticate fine — the
    DB row remains the source of truth for org/role."""
    import jwt as _jwt
    from app.config import settings as s
    from datetime import timedelta

    db = _fresh_db(tmp_path)
    u = _make_user(db, "legacy@corp.io", org_id="org-corp")
    legacy = _jwt.encode(
        {"sub": u.id, "type": "access", "jti": "legacy-jti",
         "exp": auth_service._utcnow() + timedelta(hours=1),
         "iat": auth_service._utcnow()},
        s.JWT_SECRET, algorithm=s.JWT_ALGORITHM,
    )
    assert auth_service.verify_token(legacy, db) == u.id
    claims = auth_service.verify_token_claims(legacy, db)
    assert claims.get("org_id") is None  # absent — caller falls back to DB


# ── admin surface is org-scoped ───────────────────────────────────────────


async def test_admin_list_scoped_to_own_org(tmp_path):
    from app.routers.admin_users import list_users

    db = _fresh_db(tmp_path)
    admin_a = _make_user(db, "a-admin@corp.io", role="admin", org_id="org-a")
    _make_user(db, "a-user@corp.io", role="user", org_id="org-a")
    _make_user(db, "b-admin@other.io", role="admin", org_id="org-b")
    _make_user(db, "b-user@other.io", role="user", org_id="org-b")

    visible = await list_users(db=db, admin=admin_a)
    emails = {u["email"] for u in visible}
    assert emails == {"a-admin@corp.io", "a-user@corp.io"}  # org-b invisible


async def test_cross_org_user_is_invisible_404(tmp_path):
    from app.routers.admin_users import get_user

    db = _fresh_db(tmp_path)
    admin_a = _make_user(db, "a-admin@corp.io", role="admin", org_id="org-a")
    victim_b = _make_user(db, "b-user@other.io", role="user", org_id="org-b")

    with pytest.raises(HTTPException) as e:
        await get_user(victim_b.id, db=db, admin=admin_a)
    assert e.value.status_code == 404  # existence hidden, not 403


async def test_created_user_joins_admin_org(tmp_path):
    from app.routers.admin_users import create_user, CreateUserRequest

    db = _fresh_db(tmp_path)
    admin_a = _make_user(db, "a-admin@corp.io", role="admin", org_id="org-a")
    created = await create_user(
        CreateUserRequest(email="new@corp.io", full_name="New",
                          password="Passw0rd!123", role="user"),
        db=db, admin=admin_a,
    )
    from app.models.user import User

    u = db.query(User).filter(User.email == "new@corp.io").first()
    assert u.org_id == "org-a"


async def test_normal_user_gets_403(tmp_path):
    """The admin routers enforce the role via the require_org_admin
    DEPENDENCY — direct calls skip FastAPI dependency injection, so the
    guard itself is what the unit test exercises."""
    from app.deps import require_org_admin

    db = _fresh_db(tmp_path)
    pleb = _make_user(db, "pleb@corp.io", role="user", org_id="org-a")
    with pytest.raises(HTTPException) as e:
        require_org_admin(user=pleb)
    assert e.value.status_code == 403
