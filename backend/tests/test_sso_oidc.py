"""Tests for SSO / OIDC (roadmap item 7) — provider config, ID-token
verification (real RSA), state store, account linking, and router flow.

No live IdP: discovery/JWKS/token HTTP is mocked; ID tokens are signed
with a test RSA key and verified for real (PyJWT + JWKS).
"""

from __future__ import annotations

import base64
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings  # noqa: E402

_ISSUER = "https://accounts.example.com"
_CLIENT_ID = "client-123"
_CLIENT_SECRET = "secret-abc"

_PROVIDER_CFG = {
    "id": "example",
    "name": "Example IdP",
    "client_id": _CLIENT_ID,
    "client_secret": _CLIENT_SECRET,
    "issuer": _ISSUER,
}

_DISCOVERY = {
    "issuer": _ISSUER,
    "authorization_endpoint": f"{_ISSUER}/authorize",
    "token_endpoint": f"{_ISSUER}/token",
    "jwks_uri": f"{_ISSUER}/jwks",
}


# ── Crypto helpers (test RSA key + JWT signing) ────────────────────────────

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _make_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key",
        "n": _b64u(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64u(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }
    return key, {"keys": [jwk]}


def _sign(key, claims: dict, *, kid: str = "test-key") -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def _claims(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    claims = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "sub": "user-abc",
        "email": "alice@example.com",
        "email_verified": True,
        "name": "Alice",
        "nonce": "nonce-1",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return claims


# ── Provider config ────────────────────────────────────────────────────────

class TestProviderConfig:
    def test_disabled_returns_no_providers(self):
        from app.services.sso.oidc import get_providers
        with patch.object(settings, "SSO_ENABLED", False), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]):
            assert get_providers() == []

    def test_parses_providers_from_settings(self):
        from app.services.sso.oidc import get_provider, get_providers
        with patch.object(settings, "SSO_ENABLED", True), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]):
            providers = get_providers()
            assert len(providers) == 1
            assert providers[0].id == "example"
            assert providers[0].client_id == _CLIENT_ID
            p = get_provider("example")
            assert p is not None and p.issuer == _ISSUER

    def test_unknown_provider_returns_none(self):
        from app.services.sso.oidc import get_provider
        with patch.object(settings, "SSO_ENABLED", True), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]):
            assert get_provider("nope") is None

    def test_discovery_url_defaults_from_issuer(self):
        from app.services.sso.oidc import SsoProvider
        p = SsoProvider(**_PROVIDER_CFG)
        assert p.well_known_url == f"{_ISSUER}/.well-known/openid-configuration"


# ── State store ────────────────────────────────────────────────────────────

class TestStateStore:
    def test_create_consume_roundtrip(self):
        from app.services.sso.state import SsoStateStore
        store = SsoStateStore(ttl_seconds=600)
        entry = store.create("example", next_url="/chat")
        assert entry["state"]
        assert entry["nonce"]
        consumed = store.consume(entry["state"])
        assert consumed is not None
        assert consumed["provider_id"] == "example"
        assert consumed["nonce"] == entry["nonce"]
        assert consumed["next_url"] == "/chat"

    def test_consume_once_only(self):
        from app.services.sso.state import SsoStateStore
        store = SsoStateStore(ttl_seconds=600)
        entry = store.create("example")
        assert store.consume(entry["state"]) is not None
        assert store.consume(entry["state"]) is None

    def test_expired_state_rejected(self):
        from app.services.sso.state import SsoStateStore
        store = SsoStateStore(ttl_seconds=0)
        entry = store.create("example")
        assert store.consume(entry["state"]) is None

    def test_unknown_state_rejected(self):
        from app.services.sso.state import SsoStateStore
        store = SsoStateStore(ttl_seconds=600)
        assert store.consume("bogus-state") is None


# ── ID token verification (real RSA) ──────────────────────────────────────

class TestIdTokenVerification:
    def _verify(self, id_token: str, *, nonce: str = "nonce-1", jwks=None):
        from app.services.sso.oidc import SsoProvider, verify_id_token
        if jwks is None:
            _, jwks = _make_keypair()
        provider = SsoProvider(**_PROVIDER_CFG)
        return verify_id_token(id_token, provider=provider, jwks=jwks, nonce=nonce)

    def test_valid_id_token_returns_claims(self):
        key, jwks = _make_keypair()
        claims = self._verify(_sign(key, _claims()), jwks=jwks)
        assert claims is not None
        assert claims["email"] == "alice@example.com"

    def test_wrong_audience_rejected(self):
        key, jwks = _make_keypair()
        assert self._verify(_sign(key, _claims(aud="other-client")), jwks=jwks) is None

    def test_wrong_issuer_rejected(self):
        key, jwks = _make_keypair()
        assert self._verify(_sign(key, _claims(iss="https://evil.example.com")), jwks=jwks) is None

    def test_expired_rejected(self):
        key, jwks = _make_keypair()
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        assert self._verify(_sign(key, _claims(iat=past, exp=past + timedelta(minutes=1))), jwks=jwks) is None

    def test_nonce_mismatch_rejected(self):
        key, jwks = _make_keypair()
        assert self._verify(_sign(key, _claims()), nonce="other-nonce", jwks=jwks) is None

    def test_bad_signature_rejected(self):
        _, jwks = _make_keypair()
        other_key, _ = _make_keypair()
        assert self._verify(_sign(other_key, _claims()), jwks=jwks) is None

    def test_missing_email_rejected(self):
        key, jwks = _make_keypair()
        assert self._verify(_sign(key, _claims(email=None)), jwks=jwks) is None

    def test_unverified_email_rejected(self):
        key, jwks = _make_keypair()
        assert self._verify(_sign(key, _claims(email_verified=False)), jwks=jwks) is None


# ── Account linking (real sqlite) ─────────────────────────────────────────

@pytest.fixture
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


class TestAccountLinking:
    def test_new_user_created(self, db):
        from app.services.sso.account import find_or_create_sso_user
        user = find_or_create_sso_user(
            db, email="new@example.com", full_name="New User", provider_id="example")
        assert user is not None
        assert user.email == "new@example.com"
        assert user.password_hash  # unusable random hash, never empty

    def test_existing_user_linked_no_duplicate(self, db):
        from app.models.user import User
        from app.services.sso.account import find_or_create_sso_user
        existing = User(email="same@example.com", full_name="Local User",
                        password_hash="x", role="user")
        db.add(existing)
        db.commit()
        user = find_or_create_sso_user(
            db, email="same@example.com", full_name="Local User", provider_id="example")
        assert user.id == existing.id
        assert db.query(User).filter(User.email == "same@example.com").count() == 1

    def test_domain_allowlist_allows_exact_and_subdomain(self, db):
        from app.services.sso.account import find_or_create_sso_user, is_email_allowed
        with patch.object(settings, "SSO_ALLOWED_EMAIL_DOMAINS", ["example.com"]):
            assert is_email_allowed("a@example.com") is True
            assert is_email_allowed("a@sub.example.com") is True
            assert is_email_allowed("a@notexample.com") is False
            assert find_or_create_sso_user(
                db, email="a@notexample.com", full_name="X", provider_id="example") is None

    def test_empty_allowlist_allows_all(self, db):
        from app.services.sso.account import is_email_allowed
        with patch.object(settings, "SSO_ALLOWED_EMAIL_DOMAINS", []):
            assert is_email_allowed("a@anything.io") is True


# ── Router flow (direct calls, mocked HTTP) ───────────────────────────────

def _fake_user():
    class _FakeUser:
        id = "user-1"

        def to_dict(self):
            return {"id": "user-1", "email": "alice@example.com", "role": "user"}
    return _FakeUser()


class TestSsoRouter:
    def test_providers_route_empty_when_disabled(self):
        from app.routers.auth_sso import sso_providers
        with patch.object(settings, "SSO_ENABLED", False):
            resp = None
            import asyncio
            resp = asyncio.run(sso_providers("app-1"))
        assert resp == {"providers": []}

    def test_authorize_redirects_with_state_and_nonce(self):
        from app.routers.auth_sso import sso_authorize
        with patch.object(settings, "SSO_ENABLED", True), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]), \
             patch("app.routers.auth_sso.fetch_discovery",
                   return_value=_DISCOVERY):
            import asyncio
            resp = asyncio.run(sso_authorize("app-1", "example", next="/chat", db=MagicMock()))
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert _DISCOVERY["authorization_endpoint"] in location
        assert "state=" in location and "nonce=" in location
        assert "client_id=" + _CLIENT_ID in location

    def test_authorize_unknown_provider_404(self):
        from fastapi import HTTPException
        from app.routers.auth_sso import sso_authorize
        import asyncio
        with patch.object(settings, "SSO_ENABLED", True), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(sso_authorize("app-1", "nope", db=MagicMock()))
        assert exc.value.status_code == 404

    def test_callback_happy_path(self):
        key, jwks = _make_keypair()
        with patch.object(settings, "SSO_ENABLED", True), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]), \
             patch("app.routers.auth_sso.fetch_discovery",
                   return_value=_DISCOVERY), \
             patch("app.routers.auth_sso.fetch_jwks",
                   return_value=jwks):
            from app.routers.auth_sso import sso_authorize, sso_callback
            import asyncio
            auth_resp = asyncio.run(sso_authorize("app-1", "example", db=MagicMock()))
            state = _extract_param(auth_resp.headers["location"], "state")
            nonce = _extract_param(auth_resp.headers["location"], "nonce")
            id_token = _sign(key, _claims(nonce=nonce))
            from app.services.auth_service import auth_service
            with patch("app.routers.auth_sso.exchange_code",
                       return_value={"id_token": id_token, "access_token": "at"}), \
                 patch.object(auth_service, "create_refresh_token", return_value="refresh-abc"), \
                 patch("app.routers.auth_sso.find_or_create_sso_user",
                       return_value=_fake_user()):
                resp = asyncio.run(sso_callback(
                    "app-1", "example", code="code-1", state=state, db=MagicMock()))
        assert resp.status_code == 302
        target = resp.headers["location"]
        assert target.startswith("http://localhost:5157/sso/callback#")
        assert "access_token=" in target
        assert "refresh_token=refresh-abc" in target
        assert "user=" in target

    def test_callback_bad_state_rejected(self):
        from fastapi import HTTPException
        from app.routers.auth_sso import sso_callback
        import asyncio
        with patch.object(settings, "SSO_ENABLED", True), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(sso_callback("app-1", "example", code="c", state="bogus", db=MagicMock()))
        assert exc.value.status_code == 400

    def test_callback_blocks_disallowed_domain(self):
        key, jwks = _make_keypair()
        from app.services.sso.state import SsoStateStore
        store = SsoStateStore(ttl_seconds=600)
        entry = store.create("example")
        id_token = _sign(key, _claims(email="alice@evil.com", nonce=entry["nonce"]))
        from app.routers.auth_sso import sso_callback
        import asyncio
        with patch.object(settings, "SSO_ENABLED", True), \
             patch.object(settings, "SSO_PROVIDERS", [_PROVIDER_CFG]), \
             patch.object(settings, "SSO_ALLOWED_EMAIL_DOMAINS", ["example.com"]), \
             patch("app.routers.auth_sso.fetch_discovery",
                   return_value=_DISCOVERY), \
             patch("app.routers.auth_sso.fetch_jwks", return_value=jwks), \
             patch("app.routers.auth_sso.exchange_code",
                   return_value={"id_token": id_token}), \
             patch("app.routers.auth_sso.sso_state_store", store):
            resp = asyncio.run(sso_callback(
                "app-1", "example", code="c", state=entry["state"], db=MagicMock()))
        assert resp.status_code == 302
        assert "error=" in resp.headers["location"]
        assert "access_token=" not in resp.headers["location"]


def _extract_param(url: str, name: str) -> str:
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(url).query)
    return qs[name][0]
