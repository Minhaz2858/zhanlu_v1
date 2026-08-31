"""Tests for passwordless email-code login (plan 2026-08-31).

Covers:
  * request-login-code returns a generic success for both known and unknown emails,
    only generating + emailing a code for registered accounts.
  * login-with-code issues a token pair on a valid login code.
  * login and registration OTPs are isolated by `purpose` (cross-purpose reuse fails).
  * a login code is single-use (cannot be replayed).
"""
import pytest
from fastapi import HTTPException

from app.config import settings
from app.services.auth_service import auth_service


def _fresh_db(tmp_path):
    """Build an isolated SQLite engine + session and create the schema from the models."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401 — register every model on Base.metadata
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/email_code_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_user(db, email="user@example.com"):
    from app.models.user import User
    u = User(email=email, full_name="Tester", role="user", password_hash="h")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


async def _stub_smtp(monkeypatch):
    """Replace send_otp_email with a recorder that returns True (no real send)."""
    sent = []

    async def _fake(to_addr, otp, purpose="verify your account"):
        sent.append((to_addr, otp, purpose))
        return True

    monkeypatch.setattr("app.routers.auth.send_otp_email", _fake)
    return sent


async def test_request_login_code_known_email_sends_and_stores(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    _make_user(db, "known@example.com")
    sent = await _stub_smtp(monkeypatch)

    from app.routers.auth import request_login_code
    resp = await request_login_code("app", {"email": "known@example.com"}, db)
    assert resp == {"sent": True}

    # A login-purpose OTP was generated and emailed.
    assert len(sent) == 1
    addr, code, purpose = sent[0]
    assert addr == "known@example.com"
    assert purpose == "log in to your account"
    assert len(code) == 6

    # It is persisted with purpose="login".
    from app.models.otp_code import OtpCode
    rec = db.query(OtpCode).filter(OtpCode.email == "known@example.com", OtpCode.purpose == "login").first()
    assert rec is not None and rec.code == code


async def test_request_login_code_unknown_email_generic_success(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    sent = await _stub_smtp(monkeypatch)

    from app.routers.auth import request_login_code
    resp = await request_login_code("app", {"email": "nobody@example.com"}, db)
    # Generic success — never reveals the email is unregistered.
    assert resp == {"sent": True}
    # No code generated/emailed for an unknown email.
    assert sent == []
    from app.models.otp_code import OtpCode
    assert db.query(OtpCode).filter(OtpCode.email == "nobody@example.com").first() is None


async def test_request_login_code_missing_email_400(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    await _stub_smtp(monkeypatch)
    from app.routers.auth import request_login_code
    with pytest.raises(HTTPException) as exc:
        await request_login_code("app", {}, db)
    assert exc.value.status_code == 400


async def test_login_with_code_success(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    u = _make_user(db, "login@example.com")
    await _stub_smtp(monkeypatch)

    # Generate a real login code via the service, then log in with it.
    code = auth_service.generate_otp(u.email, db, purpose="login")

    from app.routers.auth import login_with_code
    resp = await login_with_code("app", {"email": u.email, "code": code}, db)
    assert "access_token" in resp and "refresh_token" in resp
    assert resp["user"]["email"] == u.email
    # Token actually validates.
    assert auth_service.verify_token(resp["access_token"], db) == u.id


async def test_login_with_code_invalid_fails(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    u = _make_user(db, "bad@example.com")
    await _stub_smtp(monkeypatch)
    auth_service.generate_otp(u.email, db, purpose="login")

    from app.routers.auth import login_with_code
    with pytest.raises(HTTPException) as exc:
        await login_with_code("app", {"email": u.email, "code": "000000"}, db)
    assert exc.value.status_code == 401


async def test_login_otp_isolated_from_registration(tmp_path, monkeypatch):
    """A registration-purpose OTP must NOT log a user in (purpose isolation)."""
    db = _fresh_db(tmp_path)
    u = _make_user(db, "iso@example.com")
    await _stub_smtp(monkeypatch)
    reg_code = auth_service.generate_otp(u.email, db, purpose="registration")

    from app.routers.auth import login_with_code
    with pytest.raises(HTTPException) as exc:
        await login_with_code("app", {"email": u.email, "code": reg_code}, db)
    assert exc.value.status_code == 401


async def test_login_code_single_use(tmp_path, monkeypatch):
    """A login code cannot be replayed after a successful login."""
    db = _fresh_db(tmp_path)
    u = _make_user(db, "replay@example.com")
    await _stub_smtp(monkeypatch)
    code = auth_service.generate_otp(u.email, db, purpose="login")

    from app.routers.auth import login_with_code
    await login_with_code("app", {"email": u.email, "code": code}, db)
    # Second attempt with the same (now used) code must fail.
    with pytest.raises(HTTPException) as exc:
        await login_with_code("app", {"email": u.email, "code": code}, db)
    assert exc.value.status_code == 401
