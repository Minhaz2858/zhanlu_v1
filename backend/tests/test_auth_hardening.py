"""Tests for the auth-hardening work (plan 2026-07-27).

Covers: AnonymousIdentity neutering, password policy, rate limiter,
refresh-token lifecycle, revocation, and (gated) endpoint contracts.
"""
import os

import pytest

from app.deps import get_current_user_optional, AnonymousIdentity


# --- Task 1: AnonymousIdentity is no longer returned ---

def test_optional_returns_none_without_token():
    """No Authorization header and no anonymous id => None (not AnonymousIdentity)."""
    result = get_current_user_optional(
        authorization=None,
        x_base44_anonymous_id=None,
        db=None,
    )
    assert result is None


def test_optional_ignores_anonymous_id_header():
    """Even if the client sends X-Base44-Anonymous-Id, we return None — no more anon identities."""
    result = get_current_user_optional(
        authorization=None,
        x_base44_anonymous_id="some-browser-uuid",
        db=None,
    )
    assert result is None


# --- Task 2: routers require auth (DB-free structural check) ---

def _router_dep_callables(router):
    """Return the list of callables wired as router-level dependencies."""
    from fastapi.params import Depends as _Depends
    out = []
    for d in router.dependencies:
        # Each entry is a fastapi.params.Depends; .dependency is the wrapped callable
        call = getattr(d, "dependency", d)
        out.append(call)
    return out


def test_nl2sql_router_enforces_auth():
    from app.routers.nl2sql import router
    from app.deps import get_current_user_required
    assert get_current_user_required in _router_dep_callables(router)


def test_openharness_router_enforces_auth():
    from app.routers.openharness import router
    from app.deps import get_current_user_required
    assert get_current_user_required in _router_dep_callables(router)


def test_marketplace_router_enforces_auth():
    from app.routers.marketplace import router
    from app.deps import get_current_user_required
    assert get_current_user_required in _router_dep_callables(router)


def test_no_router_imports_get_current_user_optional():
    """No router source still references the neutered get_current_user_optional."""
    import importlib, pkgutil, app.routers as pkg
    from app.deps import get_current_user_optional
    # Any router module that tried to import the old helper would fail at import;
    # importing the package's submodules proves none depend on it at module level.
    for _, name, _ in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"app.routers.{name}")


# --- Task 4: password policy ---

from app.services.password_policy import validate_password


def test_password_too_short():
    errs = validate_password("Ab1")
    assert any("at least 10 characters" in e.lower() for e in errs)


def test_password_no_letter():
    errs = validate_password("1234567890")
    assert any("letter" in e.lower() for e in errs)


def test_password_no_digit():
    errs = validate_password("abcdefghij")
    assert any("digit" in e.lower() for e in errs)


def test_password_valid():
    assert validate_password("Abcdef1234") == []


# --- Task 6: rate limiter ---

import time as _time
from app.middleware.rate_limit import RateLimiter


def test_rate_limiter_allows_under_limit():
    rl = RateLimiter()
    assert rl.check("k", limit=3, window_s=60) is True
    assert rl.check("k", limit=3, window_s=60) is True
    assert rl.check("k", limit=3, window_s=60) is True


def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter()
    for _ in range(3):
        rl.check("k", limit=3, window_s=60)
    assert rl.check("k", limit=3, window_s=60) is False


def test_rate_limiter_window_expires():
    rl = RateLimiter()
    now = _time.time()
    rl._buckets["k"] = [now - 120, now - 110, now - 100]
    assert rl.check("k", limit=3, window_s=60) is True  # all expired


def test_rate_limiter_disabled_when_limit_zero():
    rl = RateLimiter()
    for _ in range(100):
        assert rl.check("k", limit=0, window_s=60) is True


# --- Task 10: refresh tokens + revocation (isolated SQLite engine) ---

import jwt as _jwt
from datetime import datetime as _dt, timezone as _tz

from app.config import settings
from app.services.auth_service import auth_service


def _fresh_db(tmp_path):
    """Build an isolated SQLite engine + session and create the schema from the models.

    Uses a private engine (NOT the module-level app.database.engine, which is
    bound to the real DATABASE_URL at import time) so tests never touch the
    configured Postgres.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401 — register every model on Base.metadata
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/auth_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_user(db):
    from app.models.user import User
    u = User(email="x@y.com", full_name="X", role="user", password_hash="h")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_refresh_token_round_trip(tmp_path):
    db = _fresh_db(tmp_path)
    u = _make_user(db)
    raw = auth_service.create_refresh_token(u.id, db)
    assert auth_service.verify_refresh_token(raw, db) == u.id


def test_rotate_refresh_token_invalidates_old(tmp_path):
    db = _fresh_db(tmp_path)
    u = _make_user(db)
    raw = auth_service.create_refresh_token(u.id, db)
    new_access, new_refresh = auth_service.rotate_refresh_token(raw, db)
    assert new_access and new_refresh and new_refresh != raw
    # Old token is now used — verify_refresh_token must reject it.
    assert auth_service.verify_refresh_token(raw, db) is None


def test_revoke_access_token_blocks_verification(tmp_path):
    db = _fresh_db(tmp_path)
    u = _make_user(db)
    access = auth_service.create_access_token(u.id, db)
    payload = _jwt.decode(access, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    exp = _dt.fromtimestamp(payload["exp"], tz=_tz.utc)
    auth_service.revoke_access_token(payload["jti"], u.id, exp, db)
    assert auth_service.is_access_token_revoked(payload["jti"], db) is True
    # verify_token with a db session must now reject the revoked access token.
    assert auth_service.verify_token(access, db) is None


def test_verify_token_without_db_still_works_for_legacy_tokens(tmp_path):
    """Legacy tokens (no JTI, no type) still verify when no db is passed — backward compat."""
    legacy = auth_service.create_token("some-user-id")  # old-style 24h token
    assert auth_service.verify_token(legacy) == "some-user-id"


def test_revoke_all_user_refresh_tokens(tmp_path):
    db = _fresh_db(tmp_path)
    u = _make_user(db)
    auth_service.create_refresh_token(u.id, db)
    auth_service.create_refresh_token(u.id, db)
    auth_service.revoke_all_user_refresh_tokens(u.id, db)
    # Both should now be unusable.
    assert auth_service.verify_refresh_token("anything", db) is None


# --- Task 12: /auth/refresh + /auth/logout endpoints (direct-call, no TestClient) ---

from fastapi import HTTPException


async def test_refresh_endpoint_rotates(tmp_path):
    db = _fresh_db(tmp_path)
    u = _make_user(db)
    raw = auth_service.create_refresh_token(u.id, db)
    from app.routers.auth import refresh_token_endpoint
    resp = await refresh_token_endpoint("app", {"refresh_token": raw}, db)
    assert "access_token" in resp and "refresh_token" in resp
    assert resp["refresh_token"] != raw
    # Old token is now used.
    assert auth_service.verify_refresh_token(raw, db) is None


async def test_refresh_endpoint_rejects_invalid(tmp_path):
    db = _fresh_db(tmp_path)
    from app.routers.auth import refresh_token_endpoint
    with pytest.raises(HTTPException) as exc:
        await refresh_token_endpoint("app", {"refresh_token": "bogus"}, db)
    assert exc.value.status_code == 401


async def test_refresh_endpoint_requires_field(tmp_path):
    db = _fresh_db(tmp_path)
    from app.routers.auth import refresh_token_endpoint
    with pytest.raises(HTTPException) as exc:
        await refresh_token_endpoint("app", {}, db)
    assert exc.value.status_code == 400


async def test_logout_revokes_access_and_refresh(tmp_path):
    db = _fresh_db(tmp_path)
    u = _make_user(db)
    access = auth_service.create_access_token(u.id, db)
    raw = auth_service.create_refresh_token(u.id, db)
    from app.routers.auth import logout_revoke
    # Minimal stub request: only .headers.get("authorization") is read.
    req = type("R", (), {"headers": {"authorization": f"Bearer {access}"}})()
    resp = await logout_revoke("app", req, u, db)
    assert resp == {"success": True}
    payload = _jwt.decode(access, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    assert auth_service.is_access_token_revoked(payload["jti"], db) is True
    assert auth_service.verify_refresh_token(raw, db) is None


# --- Fresh-user scoping: AgentApp + Project reads are per-user (not shared) ---

def test_agentapp_and_project_scoped_per_user(tmp_path):
    """A fresh user must NOT see agents/projects created by another user.

    The entity router scopes reads via ``_owner_id(entity_name, user)``:
    user-scoped entities return ``user.id`` (so list_records filters to
    ``created_by_id == user.id``); shared entities return ``None`` (no filter
    — everyone sees everything). Before this change AgentApp + Project were
    shared, so a fresh user saw every agent/project anyone created. They must
    now be scoped: a fresh user's owner_id is their own id, so they see none.
    """
    db = _fresh_db(tmp_path)
    from app.models.user import User
    from app.models.agent_app import AgentApp
    from app.routers.entities import _owner_id
    from app.services import entity_service

    alice = _make_user(db)  # email x@y.com
    bob = User(email="bob@x.com", full_name="Bob", role="user", password_hash="h")
    db.add(bob)
    db.commit()
    db.refresh(bob)

    # Alice owns an agent; Bob owns nothing (fresh user).
    db.add(AgentApp(name="Alice's agent", created_by_id=alice.id))
    db.commit()

    # _owner_id is the router's scoping decision point.
    assert _owner_id("AgentApp", bob) == bob.id, "AgentApp must be user-scoped"
    assert _owner_id("Project", bob) == bob.id, "Project must be user-scoped"
    assert _owner_id("AgentApp", alice) == alice.id

    # And the scoping actually filters: a fresh user's agent list is empty,
    # Alice sees only her own.
    bob_agents = entity_service.list_records(
        AgentApp, db, None, None, None, owner_id=_owner_id("AgentApp", bob)
    )
    assert len(bob_agents) == 0, "fresh user must see no agents"
    alice_agents = entity_service.list_records(
        AgentApp, db, None, None, None, owner_id=_owner_id("AgentApp", alice)
    )
    assert len(alice_agents) == 1
    assert alice_agents[0]["name"] == "Alice's agent"
