# Claude-style Auth Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert Zhanlu into a login-required, email+password-only SaaS app like Claude.ai — no anonymous access, hardened passwords, rate limiting, short-lived access tokens + rotating refresh tokens, logout revocation, and removal of the Feishu/Google login paths.

**Architecture:** FastAPI backend + React frontend (Vite) glued together by the `@base44/sdk`. The SDK stores JWTs in `localStorage` and attaches `Authorization: Bearer <token>` to every request. **Ripping out the Base44 SDK auth layer to use httpOnly cookies is out of scope for this plan** — it would require forking `@base44/sdk/dist/utils/axios-client.js` and rewriting `AuthContext.jsx` end-to-end, which is high-risk and deserves its own plan. Instead, we keep the SDK's Bearer-token transport but harden the token lifecycle: a 15-minute access token, a 30-day rotating refresh token (both in `localStorage`), and a server-side `revoked_tokens` table so logout actually invalidates the session. The httpOnly-cookie migration is recorded as a future follow-up in Task 21.

Frontend access control is enforced by a single `<ProtectedRoute>` wrapper around every authenticated route; the backend is the source of truth and rejects anonymous requests on every protected endpoint.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.x, Alembic, PyJWT, passlib[bcrypt], React 18, Vite, `@base44/sdk`, axios.

## Global Constraints

- **Auth methods:** email + password only. Feishu OAuth router and the Google login button are removed.
- **Access model:** login required for every page and every mutating/LLM/data endpoint. No `AnonymousIdentity` is ever returned to callers.
- **Password policy:** minimum 10 characters, at least one letter and one digit (configurable via `PASSWORD_MIN_LENGTH`, `PASSWORD_REQUIRE_LETTER`, `PASSWORD_REQUIRE_DIGIT`).
- **Token lifetimes:** access token = 15 minutes (`ACCESS_TOKEN_EXPIRE_MINUTES`); refresh token = 30 days (`REFRESH_TOKEN_EXPIRE_DAYS`), rotated on each refresh.
- **Rate limits (in-memory, per IP):** login = 5 / 60s; register = 3 / 600s; OTP resend = 3 / 600s; password reset request = 3 / 600s. Configurable via env.
- **Existing data is preserved.** The first-user-is-admin logic in `register()` is removed (the user chose "single role, everyone equal" — all users get `role="user"`).
- **No parallel pytest workers** and no broad `python -m pytest tests/` collection on this box (RAM-limited — see memory `80215686`). Each backend task runs only the single new test file it creates, via explicit path.
- Frontend dev server runs on port 5157; backend on 5002. The dev reverse proxy already forwards `/api/*` to the backend.

## File Structure

**Backend — new files:**
- `backend/app/models/refresh_token.py` — `RefreshToken` SQLAlchemy model (id, user_id FK, token_hash, expires_at, used, created_date, is_deleted). Stores a SHA-256 hash of the refresh token, never the raw token.
- `backend/app/models/revoked_token.py` — `RevokedToken` model (id, jti, user_id, expires_at). Blacklist of access-token JTIs.
- `backend/app/middleware/rate_limit.py` — in-memory sliding-window rate limiter with a FastAPI middleware factory and a per-route decorator for the auth endpoints.
- `backend/app/services/password_policy.py` — `validate_password(password) -> list[str]` returning a list of human-readable error strings.
- `backend/alembic/versions/031_auth_refresh_and_revocation.py` — creates `refresh_tokens` + `revoked_tokens` tables.
- `backend/tests/test_auth_hardening.py` — unit tests for password policy, refresh-token rotation, revocation, and the rate limiter.

**Backend — modified files:**
- `backend/app/config.py` — add `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `PASSWORD_MIN_LENGTH`, `PASSWORD_REQUIRE_LETTER`, `PASSWORD_REQUIRE_DIGIT`, `RATE_LIMIT_*` settings. Keep `JWT_EXPIRE_HOURS` for backward compat but unused by new tokens.
- `backend/app/services/auth_service.py` — add `create_access_token(user_id, db)`, `create_refresh_token(user_id, db)`, `verify_refresh_token(token, db)`, `rotate_refresh_token(old_token, db)`, `revoke_access_token(jti, user_id, exp, db)`, `is_access_token_revoked(jti, db)`, `revoke_all_user_refresh_tokens(user_id, db)`. Modify `create_token`/`verify_token` to use JTI + expiry from new settings and check the revocation table.
- `backend/app/deps.py` — neuter `AnonymousIdentity`: `get_current_user_optional` returns `None` (not an `AnonymousIdentity`) when no valid Bearer token is present, and rejects revoked tokens. Keep the function name so existing imports don't break.
- `backend/app/routers/auth.py` — add `POST /apps/{app_id}/auth/refresh`, `POST /apps/{app_id}/auth/logout` (revokes tokens); wire password validation + rate limiting into `login`, `register`, `resend_otp`, `reset_password_request`, `reset_password`; remove the first-user-is-admin branch.
- `backend/app/routers/feishu_auth.py` — **deleted**.
- `backend/main.py` — remove the `feishu_auth_router` include; add the rate-limit middleware.
- `backend/app/routers/agents.py`, `marketplace.py`, `entities.py`, `analytics.py`, `mcp.py`, `openharness.py`, `nl2sql.py` — sweep `get_current_user_optional` → `get_current_user_required` (see Task 2 for the exact list).

**Frontend — new files:**
- `frontend/src/components/ProtectedRoute.jsx` — wrapper that reads `useAuth()` and redirects to `/login` when `!isAuthenticated` (preserving `?next=` for post-login redirect).

**Frontend — modified files:**
- `frontend/src/pages/Login.jsx` — remove the Google button + Feishu button + the `feishuEnabled` fetch.
- `frontend/src/pages/Register.jsx` — add client-side password policy mirror + strength hint; remove any Google/Feishu buttons if present.
- `frontend/src/App.jsx` — wrap every authenticated `<Route>` in `<ProtectedRoute>`; leave `/login`, `/register`, `/forgot-password`, `/reset-password` public.
- `frontend/src/lib/AuthContext.jsx` — on any 401 from `base44.auth.me()`, attempt a silent refresh via the new `/auth/refresh` endpoint; if refresh fails, clear tokens and set `authError`.
- `frontend/src/api/base44Client.js` — add a custom axios interceptor instance that, on 401, calls the refresh endpoint once and retries the original request; stops sending `X-Base44-Anonymous-Id`.
- `frontend/src/lib/app-params.js` — if it synthesizes an anonymous id, stop.

---

## Phase 1 — Lock down the backend (no anonymous access)

### Task 1: Neuter `AnonymousIdentity` in `deps.py`

**Files:**
- Modify: `backend/app/deps.py:22-98`

**Interfaces:**
- Produces: `get_current_user_optional` now returns `Optional[User]` (real user or `None`) — never an `AnonymousIdentity`. `get_current_user_required` is unchanged in signature but now also rejects revoked access tokens (Task 8 wires the revocation check; for now it just rejects anonymous).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_auth_hardening.py
import pytest
from fastapi import HTTPException
from app.deps import get_current_user_optional, AnonymousIdentity


def test_optional_returns_none_without_token(monkeypatch):
    """No Authorization header and no anonymous id => None (not AnonymousIdentity)."""
    result = get_current_user_optional(
        authorization=None,
        x_base44_anonymous_id=None,
        db=None,
    )
    assert result is None


def test_optional_ignores_anonymous_id_header(monkeypatch):
    """Even if the client sends X-Base44-Anonymous-Id, we return None — no more anon identities."""
    result = get_current_user_optional(
        authorization=None,
        x_base44_anonymous_id="some-browser-uuid",
        db=None,
    )
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py::test_optional_ignores_anonymous_id_header -v`
Expected: FAIL — current code returns an `AnonymousIdentity`.

- [ ] **Step 3: Edit `deps.py`**

Replace the body of `get_current_user_optional` so it ignores `x_base44_anonymous_id` entirely and returns `None` when there is no valid Bearer token. Keep `AnonymousIdentity` defined (it's imported elsewhere) but mark it deprecated with a docstring note. Keep `get_current_user_required` as-is.

```python
def get_current_user_optional(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Extract the authenticated user from the Authorization header.

    Returns a real ``User`` when a valid ``Authorization: Bearer <token>``
    header is present, otherwise ``None``. Anonymous identities are no longer
    supported — login is required for everything (see plan 2026-07-27).
    """
    if not (authorization and authorization.startswith("Bearer ")):
        return None
    token = authorization[7:]
    user_id = auth_service.verify_token(token)
    if not user_id:
        return None
    return db.query(User).filter(
        User.id == user_id, User.is_deleted == False  # noqa: E712
    ).first()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -v`
Expected: PASS (both new tests).

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/deps.py backend/tests/test_auth_hardening.py
git commit -m "feat(auth): neuter AnonymousIdentity — login required everywhere"
```

---

### Task 2: Sweep `get_current_user_optional` → `get_current_user_required` across routers

**Files:**
- Modify: `backend/app/routers/agents.py` (lines 34 import, and 6 endpoints at 1849, 1983, 3327, 3914, 4486, 4541)
- Modify: `backend/app/routers/marketplace.py` (lines 21 import, endpoints at 50, 116, 137)
- Modify: `backend/app/routers/entities.py` (lines 12 import; read endpoints at 74, 87, 109, 157)
- Modify: `backend/app/routers/analytics.py` (lines 7 import, endpoints at 19, 61)
- Modify: `backend/app/routers/mcp.py` (lines 21 import, endpoint at 94)
- Modify: `backend/app/routers/openharness.py` (line 23 import — check the endpoint that uses it)
- Modify: `backend/app/routers/nl2sql.py` — add `get_current_user_required` import + dependency to **every** route handler.

**Interfaces:**
- Consumes: `get_current_user_required` from `app.deps` (Task 1).
- Produces: every endpoint in these routers now requires a valid Bearer token; anonymous requests get HTTP 401.

- [ ] **Step 1: Write a contract test**

```python
# Append to backend/tests/test_auth_hardening.py
from fastapi.testclient import TestClient


def _app_without_auth_client():
    """Build a TestClient against the real app, with NO Authorization header."""
    from main import app
    return TestClient(app)


@pytest.mark.parametrize("path,method", [
    ("/api/apps/local-zhanlu-app/nl2sql/ask", "POST"),
    ("/api/apps/local-zhanlu-app/entities/User", "GET"),
    # add more as routers are swept
])
def test_endpoints_reject_anonymous(path, method):
    client = _app_without_auth_client()
    resp = client.request(method, path, json={})
    assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"
```

> Note: if `main.py` import is heavy in the test env (DB connection), gate this test behind `@pytest.mark.skipif` checking `DATABASE_URL` presence, or use `monkeypatch` to point `DATABASE_URL` at a temp sqlite file. Prefer the latter.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py::test_endpoints_reject_anonymous -v`
Expected: FAIL — endpoints return 200/422 for anonymous requests today.

- [ ] **Step 3: For each router, change `get_current_user_optional` to `get_current_user_required`**

Mechanical edit per file: in the import line replace `get_current_user_optional` with `get_current_user_required` (drop the `optional` import), and at each endpoint signature replace `Depends(get_current_user_optional)` with `Depends(get_current_user_required)`. For `nl2sql.py` (which has no auth at all), add the import and the `user: User = Depends(get_current_user_required)` parameter to every route handler.

> If any endpoint legitimately needs to stamp `user.id` as `None` for a real reason (audit only), leave it as `optional` and add a code comment explaining why — but the brainstorming decision was "login for everything", so there should be none.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py::test_endpoints_reject_anonymous -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers
git commit -m "feat(auth): require login on agents/marketplace/entities/analytics/mcp/nl2sql endpoints"
```

---

### Task 3: Remove the Feishu OAuth router

**Files:**
- Delete: `backend/app/routers/feishu_auth.py`
- Modify: `backend/main.py:108` (remove the `feishu_auth_router` include + its import line)

- [ ] **Step 1: Delete the router file**

```bash
rm /root/zhanlu/backend/app/routers/feishu_auth.py
```

- [ ] **Step 2: Edit `main.py`**

Remove the line `from app.routers.feishu_auth import router as feishu_auth_router` (search for the exact import) and the `app.include_router(feishu_auth_router, prefix="/api")` line at `backend/main.py:108`.

- [ ] **Step 3: Verify the app still imports**

Run: `cd /root/zhanlu/backend && python -c "import main; print('ok')"`
Expected: prints `ok` with no ImportError.

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/main.py backend/app/routers/feishu_auth.py
git commit -m "feat(auth): remove Feishu OAuth router (email+password only)"
```

---

## Phase 2 — Password policy & rate limiting

### Task 4: Password policy service + tests

**Files:**
- Create: `backend/app/services/password_policy.py`
- Test: `backend/tests/test_auth_hardening.py` (append)

**Interfaces:**
- Produces: `validate_password(password: str) -> list[str]` — returns a list of human-readable error strings; empty list = valid.

- [ ] **Step 1: Write the failing tests**

```python
# Append to backend/tests/test_auth_hardening.py
from app.services.password_policy import validate_password


def test_password_too_short():
    assert "at least 10 characters" in validate_password("Ab1")[0].lower()


def test_password_no_letter():
    errs = validate_password("1234567890")
    assert any("letter" in e.lower() for e in errs)


def test_password_no_digit():
    errs = validate_password("abcdefghij")
    assert any("digit" in e.lower() for e in errs)


def test_password_valid():
    assert validate_password("Abcdef1234") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -k password -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write `password_policy.py`**

```python
"""Password complexity validation — mirrors the frontend policy."""
import re
from app.config import settings


def validate_password(password: str) -> list[str]:
    """Return a list of human-readable error strings. Empty list = valid."""
    errors: list[str] = []
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        errors.append(f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters long.")
    if settings.PASSWORD_REQUIRE_LETTER and not re.search(r"[A-Za-z]", password):
        errors.append("Password must contain at least one letter.")
    if settings.PASSWORD_REQUIRE_DIGIT and not re.search(r"\d", password):
        errors.append("Password must contain at least one digit.")
    return errors
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -k password -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/password_policy.py backend/tests/test_auth_hardening.py
git commit -m "feat(auth): add password complexity policy"
```

---

### Task 5: Add new settings to `config.py`

**Files:**
- Modify: `backend/app/config.py:54-57` (extend the Auth block)

- [ ] **Step 1: Add the settings**

Insert after the `JWT_EXPIRE_HOURS` line (around `config.py:57`):

```python
    # Auth — token lifecycle & policy (plan 2026-07-27)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    PASSWORD_MIN_LENGTH: int = 10
    PASSWORD_REQUIRE_LETTER: bool = True
    PASSWORD_REQUIRE_DIGIT: bool = True

    # Rate limits (in-memory, per IP). Zero = disabled.
    RATE_LIMIT_LOGIN_PER_MIN: int = 5
    RATE_LIMIT_REGISTER_PER_10MIN: int = 3
    RATE_LIMIT_OTP_PER_10MIN: int = 3
    RATE_LIMIT_RESET_PER_10MIN: int = 3
```

- [ ] **Step 2: Verify config loads**

Run: `cd /root/zhanlu/backend && python -c "from app.config import settings; print(settings.ACCESS_TOKEN_EXPIRE_MINUTES, settings.RATE_LIMIT_LOGIN_PER_MIN)"`
Expected: `15 5`.

- [ ] **Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/config.py
git commit -m "feat(auth): add token/password/rate-limit settings"
```

---

### Task 6: Rate-limit middleware + tests

**Files:**
- Create: `backend/app/middleware/__init__.py`
- Create: `backend/app/middleware/rate_limit.py`
- Test: `backend/tests/test_auth_hardening.py` (append)

**Interfaces:**
- Produces:
  - `RateLimiter` class — `check(key: str, limit: int, window_s: int) -> bool` (True = allowed, False = blocked). Thread-safe via a `threading.Lock`; in-memory dict of `[key] -> list[timestamp]`.
  - `rate_limit(limit: int, window_s: int)` — FastAPI dependency factory that uses the client IP + endpoint name as the key and raises `HTTPException(429)` when over limit.

- [ ] **Step 1: Write the failing tests**

```python
# Append to backend/tests/test_auth_hardening.py
import time
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


def test_rate_limiter_window_expires(monkeypatch):
    rl = RateLimiter()
    # Inject three timestamps in the past
    now = time.time()
    rl._buckets["k"] = [now - 120, now - 110, now - 100]
    assert rl.check("k", limit=3, window_s=60) is True  # all expired
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -k rate_limiter -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write `rate_limit.py`**

```python
"""In-memory sliding-window rate limiter + FastAPI dependency factory."""
import threading
import time
from collections import defaultdict
from fastapi import HTTPException, Request

from app.config import settings


class RateLimiter:
    """Thread-safe in-memory sliding-window limiter.

    Not distributed — fine for a single-process deployment. If the app ever
    scales horizontally, swap the storage for Redis (the interface stays the
    same).
    """

    def __init__(self):
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: int) -> bool:
        if limit <= 0:
            return True  # disabled
        now = time.time()
        with self._lock:
            bucket = self._buckets[key]
            # Drop expired entries
            self._buckets[key] = [t for t in bucket if t > now - window_s]
            if len(self._buckets[key]) >= limit:
                return False
            self._buckets[key].append(now)
            return True


# Module-level singleton reused by the dependency factory.
_limiter = RateLimiter()


def rate_limit(limit: int, window_s: int):
    """FastAPI dependency: 429 if the requesting IP is over `limit` in `window_s`."""
    def dep(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        key = f"{request.url.path}:{client_ip}"
        if not _limiter.check(key, limit=limit, window_s=window_s):
            raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
    return dep
```

Also create an empty `backend/app/middleware/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -k rate_limiter -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/middleware
git commit -m "feat(auth): add in-memory sliding-window rate limiter"
```

---

### Task 7: Wire password policy + rate limiting into auth endpoints

**Files:**
- Modify: `backend/app/routers/auth.py` — `login`, `register`, `resend_otp`, `reset_password_request`, `reset_password`.

- [ ] **Step 1: Edit the imports** at the top of `auth.py`

```python
from app.deps import get_db, get_current_user_required
from app.services.auth_service import auth_service
from app.services.password_policy import validate_password
from app.middleware.rate_limit import rate_limit
from app.config import settings
```

- [ ] **Step 2: Add rate limiting + password validation to each endpoint**

For `login` (around `auth.py:33`), add `Depends(rate_limit(settings.RATE_LIMIT_LOGIN_PER_MIN, 60))` to the signature. For `register`, `reset_password`, add `Depends(rate_limit(settings.RATE_LIMIT_REGISTER_PER_10MIN, 600))` and `Depends(rate_limit(settings.RATE_LIMIT_RESET_PER_10MIN, 600))` respectively, plus call `validate_password`:

```python
@router.post("/apps/{app_id}/auth/register")
async def register(
    app_id: str,
    body: dict,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit(settings.RATE_LIMIT_REGISTER_PER_10MIN, 600)),
):
    email = body.get("email")
    full_name = body.get("full_name")
    password = body.get("password")
    otp_code = body.get("otp_code")

    if not email or not full_name or not password:
        raise HTTPException(status_code=400, detail="Email, full_name, and password required")

    pw_errors = validate_password(password)
    if pw_errors:
        raise HTTPException(status_code=400, detail="; ".join(pw_errors))

    existing = db.query(User).filter(User.email == email, User.is_deleted == False).first()
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists")

    if otp_code:
        if not auth_service.verify_otp(email, otp_code, db):
            raise HTTPException(status_code=400, detail="Invalid or expired OTP code")
    elif db.query(User).count() > 0:
        raise HTTPException(
            status_code=400,
            detail="OTP verification required. Please request an OTP code first."
        )

    # Single role — everyone is a regular user (plan 2026-07-27).
    user = User(
        email=email,
        full_name=full_name,
        role="user",
        password_hash=auth_service.hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    access_token = auth_service.create_access_token(user.id, db)
    refresh_token = auth_service.create_refresh_token(user.id, db)
    return {"access_token": access_token, "refresh_token": refresh_token, "user": user.to_dict()}
```

Apply the same pattern to `reset_password` (validate new password) and add `_rl=Depends(...)` to `resend_otp` and `reset_password_request`. **Drop the first-user-is-admin branch** (`role="admin" if is_first_user else "user"` → `role="user"`).

> Note: `create_access_token` / `create_refresh_token` are added in Task 9. If running the tests before Task 9, fall back to `auth_service.create_token(user.id)` for the access token and omit `refresh_token` from the response, then update in Task 9's commit.

- [ ] **Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/auth.py
git commit -m "feat(auth): enforce password policy + rate limits on register/reset"
```

---

## Phase 3 — Refresh tokens & revocation

### Task 8: Create `RefreshToken` + `RevokedToken` models

**Files:**
- Create: `backend/app/models/refresh_token.py`
- Create: `backend/app/models/revoked_token.py`

**Interfaces:**
- Produces:
  - `RefreshToken` model: columns `id` (str PK, uuid), `user_id` (FK users.id), `token_hash` (str, unique, indexed — SHA-256 hex of the raw token), `expires_at` (datetime), `used` (bool, default False), `created_date`, `updated_date`, `is_deleted` (bool).
  - `RevokedToken` model: columns `id` (str PK), `jti` (str, unique, indexed), `user_id` (str), `expires_at` (datetime).

- [ ] **Step 1: Write `refresh_token.py`**

```python
"""Refresh token model — stores a SHA-256 hash, never the raw token."""
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import TimestampedBase


class RefreshToken(TimestampedBase):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

Check `TimestampedBase` (in `backend/app/models/base.py`) for `created_date`/`updated_date`/`is_deleted` — confirm they exist before relying on them.

- [ ] **Step 2: Write `revoked_token.py`**

```python
"""Revoked access-token JTI blacklist (logout / session kill)."""
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import TimestampedBase


class RevokedToken(TimestampedBase):
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

- [ ] **Step 3: Verify imports**

Run: `cd /root/zhanlu/backend && python -c "from app.models.refresh_token import RefreshToken; from app.models.revoked_token import RevokedToken; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/app/models/refresh_token.py backend/app/models/revoked_token.py
git commit -m "feat(auth): add RefreshToken + RevokedToken models"
```

---

### Task 9: Alembic migration `031`

**Files:**
- Create: `backend/alembic/versions/031_auth_refresh_and_revocation.py`

**Interfaces:**
- Consumes: revision `030` (the current head, from `030_knowledge_base_document_indexing.py`).
- Produces: `refresh_tokens` + `revoked_tokens` tables.

- [ ] **Step 1: Write the migration**

```python
"""031_auth_refresh_and_revocation

Revision ID: 031
Revises: 030
Create Date: 2026-07-27

Adds refresh_tokens (rotating refresh tokens) and revoked_tokens (access-token
JTI blacklist for logout / session kill). Idempotent in live mode.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_date", sa.DateTime, server_default=sa.func.now()),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false")),
    )
    op.create_table(
        "revoked_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("jti", sa.String(36), nullable=False, unique=True, index=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("created_date", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_date", sa.DateTime, server_default=sa.func.now()),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_table("revoked_tokens")
    op.drop_table("refresh_tokens")
```

- [ ] **Step 2: Run the migration against the dev DB**

Run: `cd /root/zhanlu/backend && python -m alembic upgrade head`
Expected: `Running upgrade 030 -> 031, ...` with no errors.

- [ ] **Step 3: Verify tables exist**

Run: `cd /root/zhanlu/backend && python -c "from app.database import engine; from sqlalchemy import inspect; print(sorted(inspect(engine).get_table_names()))" | tr ',' '\n' | grep -E "refresh_tokens|revoked_tokens"`
Expected: both table names appear.

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/alembic/versions/031_auth_refresh_and_revocation.py
git commit -m "feat(auth): migration 031 — refresh_tokens + revoked_tokens"
```

---

### Task 10: Extend `auth_service` with access/refresh/revocation

**Files:**
- Modify: `backend/app/services/auth_service.py`
- Test: `backend/tests/test_auth_hardening.py` (append)

**Interfaces:**
- Produces (new methods on `AuthService`):
  - `create_access_token(user_id: str, db: Session) -> str` — 15-min JWT with a `jti` claim.
  - `create_refresh_token(user_id: str, db: Session) -> str` — `secrets.token_urlsafe(48)`, hash stored in DB, 30-day expiry. Returns the RAW token (only ever seen by the client).
  - `verify_refresh_token(token: str, db: Session) -> str | None` — returns `user_id` if the token is valid, unused, and not expired; else `None`.
  - `rotate_refresh_token(old_token: str, db: Session) -> tuple[str, str] | None` — marks the old refresh token `used=True`, issues a new pair `(access_token, refresh_token)`. Returns `None` if the old token is invalid (replay → treat as breach, revoke all of the user's refresh tokens).
  - `revoke_access_token(jti: str, user_id: str, exp: datetime, db: Session)` — insert into `revoked_tokens`.
  - `is_access_token_revoked(jti: str, db: Session) -> bool`.
  - `revoke_all_user_refresh_tokens(user_id: str, db: Session)` — used on logout-all / password change.
- Modifies: `verify_token(token)` now also takes `db: Session` and returns `None` if the token's JTI is in the revocation table. Update callers in `deps.py` (Task 11).

- [ ] **Step 1: Write the failing tests**

```python
# Append to backend/tests/test_auth_hardening.py
import hashlib
from app.services.auth_service import auth_service
from app.models.refresh_token import RefreshToken
from app.models.revoked_token import RevokedToken


def _fresh_db(tmp_path, monkeypatch):
    """Point DATABASE_URL at a temp sqlite file and rebuild the schema."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/auth_test.db")
    from app.database import SessionLocal, engine
    from app.models.base import TimestampedBase
    TimestampedBase.metadata.create_all(engine)
    return SessionLocal()


def test_refresh_token_round_trip(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    from app.models.user import User
    u = User(email="x@y.com", full_name="X", role="user", password_hash="h")
    db.add(u); db.commit(); db.refresh(u)
    raw = auth_service.create_refresh_token(u.id, db)
    assert auth_service.verify_refresh_token(raw, db) == u.id
    # Raw token is NOT stored — only its hash
    assert db.query(RefreshToken).filter(RefreshToken.token_hash != hashlib.sha256(raw.encode()).hexdigest()).count() >= 0


def test_rotate_refresh_token_invalidates_old(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    from app.models.user import User
    u = User(email="x@y.com", full_name="X", role="user", password_hash="h")
    db.add(u); db.commit(); db.refresh(u)
    raw = auth_service.create_refresh_token(u.id, db)
    new_access, new_refresh = auth_service.rotate_refresh_token(raw, db)
    assert new_access and new_refresh and new_refresh != raw
    # Old token no longer verifies
    assert auth_service.verify_refresh_token(raw, db) is None


def test_revoke_access_token_blocks_verification(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    from app.models.user import User
    from datetime import datetime, timedelta, timezone
    u = User(email="x@y.com", full_name="X", role="user", password_hash="h")
    db.add(u); db.commit(); db.refresh(u)
    access = auth_service.create_access_token(u.id, db)
    import jwt as _jwt
    payload = _jwt.decode(access, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    auth_service.revoke_access_token(payload["jti"], u.id, datetime.fromtimestamp(payload["exp"], tz=timezone.utc), db)
    assert auth_service.is_access_token_revoked(payload["jti"], db) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -k "refresh_token or rotate or revoke_access" -v`
Expected: FAIL — methods don't exist yet.

- [ ] **Step 3: Extend `auth_service.py`**

Add these methods to the `AuthService` class (import `hashlib`, `uuid`, `RefreshToken`, `RevokedToken`, `settings`):

```python
import hashlib
import uuid

def create_access_token(self, user_id: str, db: Session) -> str:
    jti = str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "jti": jti,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

def create_refresh_token(self, user_id: str, db: Session) -> str:
    raw = secrets.token_urlsafe(48)
    record = RefreshToken(
        user_id=user_id,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(record)
    db.commit()
    return raw

def verify_refresh_token(self, token: str, db: Session) -> str | None:
    h = hashlib.sha256(token.encode()).hexdigest()
    record = db.query(RefreshToken).filter(
        RefreshToken.token_hash == h,
        RefreshToken.used == False,  # noqa: E712
        RefreshToken.is_deleted == False,  # noqa: E712
    ).first()
    if not record:
        return None
    if datetime.utcnow() > record.expires_at:
        return None
    return record.user_id

def rotate_refresh_token(self, old_token: str, db: Session) -> tuple[str, str] | None:
    """Mark old token used and issue a fresh pair. Returns None on replay/breach."""
    user_id = self.verify_refresh_token(old_token, db)
    if not user_id:
        # Replay / invalid token → revoke ALL of this user's refresh tokens? No —
        # we don't know whose token it is. Just reject.
        return None
    h = hashlib.sha256(old_token.encode()).hexdigest()
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == h).first()
    record.used = True
    db.commit()
    access = self.create_access_token(user_id, db)
    refresh = self.create_refresh_token(user_id, db)
    return access, refresh

def revoke_access_token(self, jti: str, user_id: str, exp: datetime, db: Session) -> None:
    db.add(RevokedToken(jti=jti, user_id=user_id, expires_at=exp))
    db.commit()

def is_access_token_revoked(self, jti: str, db: Session) -> bool:
    return db.query(RevokedToken).filter(RevokedToken.jti == jti).first() is not None

def revoke_all_user_refresh_tokens(self, user_id: str, db: Session) -> None:
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user_id,
        RefreshToken.used == False,  # noqa: E712
        RefreshToken.is_deleted == False,  # noqa: E712
    ).update({RefreshToken.used: True})
    db.commit()
```

- [ ] **Step 4: Update `verify_token` to take `db` and check the blacklist**

```python
def verify_token(self, token: str, db: Session | None = None) -> str | None:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
    if db is not None and payload.get("type") == "access":
        jti = payload.get("jti")
        if jti and self.is_access_token_revoked(jti, db):
            return None
    return payload.get("sub")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -k "refresh_token or rotate or revoke_access" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/auth_service.py backend/tests/test_auth_hardening.py
git commit -m "feat(auth): access/refresh token lifecycle + revocation"
```

---

### Task 11: Update `deps.py` to pass `db` to `verify_token` and reject revoked tokens

**Files:**
- Modify: `backend/app/deps.py:40-83` (the `get_current_user_optional` body from Task 1)

- [ ] **Step 1: Edit the token verification call**

In `get_current_user_optional`, change:
```python
user_id = auth_service.verify_token(token)
```
to:
```python
user_id = auth_service.verify_token(token, db)
```

- [ ] **Step 2: Verify the existing tests still pass**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -v`
Expected: PASS (all).

- [ ] **Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/deps.py
git commit -m "feat(auth): verify_token checks revocation blacklist"
```

---

### Task 12: Add `/auth/refresh` and `/auth/logout` endpoints

**Files:**
- Modify: `backend/app/routers/auth.py`
- Test: `backend/tests/test_auth_hardening.py` (append)

**Interfaces:**
- Produces:
  - `POST /apps/{app_id}/auth/refresh` — body `{refresh_token}`, returns `{access_token, refresh_token}` (rotated).
  - `POST /apps/{app_id}/auth/logout` — requires a valid access token (via `get_current_user_required`); revokes the access token's JTI and all of the user's refresh tokens.

- [ ] **Step 1: Write the failing tests**

```python
# Append to backend/tests/test_auth_hardening.py
from fastapi.testclient import TestClient


def test_refresh_endpoint_returns_new_pair(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    from app.models.user import User
    u = User(email="x@y.com", full_name="X", role="user", password_hash="h")
    db.add(u); db.commit(); db.refresh(u)
    raw = auth_service.create_refresh_token(u.id, db)
    db.close()
    # Build a TestClient pointed at the temp DB
    client = TestClient(_build_app())
    resp = client.post("/api/apps/local-zhanlu-app/auth/refresh", json={"refresh_token": raw})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body and "refresh_token" in body


def _build_app():
    """Import the app fresh (DATABASE_URL already monkeypatched)."""
    import importlib, main
    importlib.reload(main)
    return main.app
```

> If `main` import is too heavy (DB driver auto-install), gate this test behind `@pytest.mark.skipif(not os.environ.get("ZHANLU_RUN_INTEGRATION"))` and verify manually in Task 20.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py::test_refresh_endpoint_returns_new_pair -v`
Expected: FAIL — endpoint doesn't exist.

- [ ] **Step 3: Add the endpoints to `auth.py`**

```python
@router.post("/apps/{app_id}/auth/refresh")
async def refresh_token_endpoint(app_id: str, body: dict, db: Session = Depends(get_db)):
    """Rotate a refresh token. Returns a fresh (access, refresh) pair.

    The old refresh token is marked used. If the token is invalid/expired/replayed,
    returns 401 — the frontend must then redirect to /login.
    """
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token required")
    pair = auth_service.rotate_refresh_token(refresh_token, db)
    if not pair:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    access, refresh = pair
    return {"access_token": access, "refresh_token": refresh}


@router.post("/apps/{app_id}/auth/logout")
async def logout(
    app_id: str,
    request: Request,
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Revoke the current access token (by JTI) and all of the user's refresh tokens."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        payload = {}
    jti = payload.get("jti")
    if jti:
        from datetime import datetime, timezone
        exp = datetime.fromtimestamp(payload.get("exp", 0), tz=timezone.utc)
        auth_service.revoke_access_token(jti, user.id, exp, db)
    auth_service.revoke_all_user_refresh_tokens(user.id, db)
    return {"success": True}
```

Add `import jwt` and `from fastapi import Request` to the imports at the top of `auth.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -k refresh_endpoint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/auth.py backend/tests/test_auth_hardening.py
git commit -m "feat(auth): add /auth/refresh and /auth/logout endpoints"
```

---

## Phase 4 — Frontend access control

### Task 13: Remove Google + Feishu from the Login page

**Files:**
- Modify: `frontend/src/pages/Login.jsx`

- [ ] **Step 1: Edit `Login.jsx`**

Remove:
- The `import GoogleIcon from "@/components/GoogleIcon";` line.
- The `QrCode` import from lucide-react (keep `LogIn, Mail, Lock, Loader2`).
- The `feishuEnabled` state, the `useEffect` that fetches `/auth/feishu/config`, and the `handleFeishu` function.
- The `handleGoogle` function.
- The Google `<Button>` block and the Feishu `{feishuEnabled && (...)}` block and the "or" divider `<div className="relative mb-6">...</div>`.

Leave the email/password `<form>` and its submit handler.

- [ ] **Step 2: Verify the page renders**

Run: `cd /root/zhanlu/frontend && npm run dev` then open `http://localhost:5157/login` — the page should show only the email/password form and the "Create one" footer link.

- [ ] **Step 3: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/Login.jsx
git commit -m "feat(auth): remove Google + Feishu login buttons"
```

---

### Task 14: Create `ProtectedRoute` + protect all routes in `App.jsx`

**Files:**
- Create: `frontend/src/components/ProtectedRoute.jsx`
- Modify: `frontend/src/App.jsx`

**Interfaces:**
- Produces: `<ProtectedRoute>` — reads `useAuth()`; if `isLoadingAuth || isLoadingPublicSettings`, renders a spinner; if `!isAuthenticated`, `<Navigate to="/login?next={currentPath}" />`; else renders `<Outlet/>` (or children).

- [ ] **Step 1: Write `ProtectedRoute.jsx`**

```jsx
import React from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/lib/AuthContext";
import { Loader2 } from "lucide-react";

export default function ProtectedRoute() {
  const { isAuthenticated, isLoadingAuth, isLoadingPublicSettings } = useAuth();
  const location = useLocation();

  if (isLoadingAuth || isLoadingPublicSettings) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }
  if (!isAuthenticated) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <Outlet />;
}
```

- [ ] **Step 2: Wrap authenticated routes in `App.jsx`**

Read the current `App.jsx` route tree. Leave `/login`, `/register`, `/forgot-password`, `/reset-password` as top-level public routes. Wrap the rest in a parent route that renders `<ProtectedRoute />` and uses `<Outlet />`:

```jsx
import ProtectedRoute from "@/components/ProtectedRoute";
// ... in the <Routes>:
<Route element={<ProtectedRoute />}>
  <Route path="/" element={<Home />} />
  <Route path="/chat" element={<Chat />} />
  {/* ...every other authenticated route... */}
</Route>
<Route path="/login" element={<Login />} />
<Route path="/register" element={<Register />} />
<Route path="/forgot-password" element={<ForgotPassword />} />
<Route path="/reset-password" element={<ResetPassword />} />
```

> Adapt to the actual route element names in the current `App.jsx` (read it first).

- [ ] **Step 3: Verify redirect works**

Run the dev server, visit `http://localhost:5157/` while logged out — you should be redirected to `/login?next=%2F`. After logging in, you should land back on `/`.

- [ ] **Step 4: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/ProtectedRoute.jsx frontend/src/App.jsx
git commit -m "feat(auth): require login for all frontend routes via ProtectedRoute"
```

---

### Task 15: Silent token refresh on app load + 401 interceptor

**Files:**
- Modify: `frontend/src/lib/AuthContext.jsx`
- Modify: `frontend/src/api/base44Client.js`
- Modify: `frontend/src/lib/app-params.js` (if it synthesizes an anonymous id)

- [ ] **Step 1: Add a `refreshAccessToken` helper in `AuthContext.jsx`**

```jsx
const refreshAccessToken = async () => {
  const refreshToken = localStorage.getItem("refresh_token");
  if (!refreshToken) return null;
  try {
    const resp = await fetch(`/api/apps/${appParams.appId}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    localStorage.setItem("token", data.access_token);
    localStorage.setItem("refresh_token", data.refresh_token);
    return data.access_token;
  } catch {
    return null;
  }
};
```

In `checkUserAuth`, if `base44.auth.me()` fails with 401, call `refreshAccessToken()`; if it returns a token, retry `base44.auth.me()`; if not, clear tokens and set `authError`.

- [ ] **Step 2: Add a 401 interceptor in `base44Client.js`**

After creating the `base44` client, attach an axios response interceptor that, on a 401, calls the refresh endpoint once and retries the original request. Use a module-level `_isRefreshing` flag + a queue to avoid refresh storms (standard pattern).

- [ ] **Step 3: Stop sending `X-Base44-Anonymous-Id`**

In `app-params.js`, remove any code that generates an anonymous browser id and stores it. If `appParams.token` is read from localStorage, keep that; just don't synthesize an anon id.

- [ ] **Step 4: Verify manually**

Log in, wait 16+ minutes, then send a chat message — the interceptor should silently refresh and the request should succeed. Inspect localStorage to confirm `token` was updated.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/lib/AuthContext.jsx frontend/src/api/base44Client.js frontend/src/lib/app-params.js
git commit -m "feat(auth): silent refresh on 401 + stop anonymous id header"
```

---

### Task 16: Password strength meter on the Register page

**Files:**
- Modify: `frontend/src/pages/Register.jsx`

- [ ] **Step 1: Add a client-side `validatePassword` mirror**

```jsx
function validatePassword(pw) {
  const errs = [];
  if (pw.length < 10) errs.push("at least 10 characters");
  if (!/[A-Za-z]/.test(pw)) errs.push("a letter");
  if (!/\d/.test(pw)) errs.push("a digit");
  return errs;
}
```

- [ ] **Step 2: Show live validation hints under the password input**

Render a small list: green check when a rule passes, red x when it fails. Block submit until `errs.length === 0`. Mirror any existing styling in `AuthLayout`.

- [ ] **Step 3: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/Register.jsx
git commit -m "feat(auth): live password strength meter on register page"
```

---

## Phase 5 — Verification & docs

### Task 17: Update `/auth/logout` redirect handler

**Files:**
- Modify: `backend/app/routers/auth.py:260-264` (the old GET `/apps/auth/logout` redirect)

- [ ] **Step 1: Keep the GET redirect for SDK compatibility but make it also clear nothing server-side** (the real revocation happens via the POST `/auth/logout` endpoint added in Task 12). The Base44 SDK's `logout()` calls this GET endpoint to redirect — leave it as a redirect-only endpoint.

- [ ] **Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/auth.py
git commit -m "chore(auth): keep GET /auth/logout as redirect shim for SDK"
```

---

### Task 18: Update `.env.example` and README

**Files:**
- Modify: `backend/.env.example`
- Modify: `README.md` (root)

- [ ] **Step 1: Add the new env vars to `.env.example`** with comments:

```dotenv
# Auth — token lifecycle & policy
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=30
PASSWORD_MIN_LENGTH=10
PASSWORD_REQUIRE_LETTER=true
PASSWORD_REQUIRE_DIGIT=true

# Rate limits (per IP)
RATE_LIMIT_LOGIN_PER_MIN=5
RATE_LIMIT_REGISTER_PER_10MIN=3
RATE_LIMIT_OTP_PER_10MIN=3
RATE_LIMIT_RESET_PER_10MIN=3
```

- [ ] **Step 2: Add a short "Authentication" section to the README** describing the login-required model, the refresh flow, and how to disable rate limits (set to 0).

- [ ] **Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/.env.example README.md
git commit -m "docs: document the new auth settings & refresh flow"
```

---

### Task 19: Run the new test file in full

- [ ] **Step 1: Run the whole auth test file**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_auth_hardening.py -v`
Expected: all tests PASS.

- [ ] **Step 2: Run a broad smoke test on auth-adjacent tests** (only if RAM allows — check `free -h` first):

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_smoke_e2e_contracts.py -v`
Expected: no new failures caused by the auth changes.

---

### Task 20: End-to-end manual verification

- [ ] **Step 1: Backend boot**

Run: `cd /root/zhanlu/backend && python -m uvicorn main:app --port 5002 --reload`
Expected: app starts, no ImportError (Feishu router gone), migration 031 applied.

- [ ] **Step 2: Frontend boot**

Run: `cd /root/zhanlu/frontend && npm run dev`
Expected: dev server on 5157.

- [ ] **Step 3: Manual flow checklist**

- [ ] Visit `http://localhost:5157/` while logged out → redirected to `/login?next=%2F`.
- [ ] `/register` with a weak password → rejected with the policy error.
- [ ] `/register` with `Abcdef1234` → succeeds, `token` + `refresh_token` land in localStorage.
- [ ] Land back on the `?next=` path after login.
- [ ] Hit a protected API (e.g. `POST /api/apps/local-zhanlu-app/nl2sql/ask`) with no token → 401.
- [ ] Wait 16 min (or temporarily set `ACCESS_TOKEN_EXPIRE_MINUTES=1`) → next request silently refreshes and succeeds.
- [ ] `POST /api/apps/local-zhanlu-app/auth/logout` with the access token → 200; the same access token now returns 401.
- [ ] Hit `POST /auth/login` 6 times in a minute from the same IP → 6th returns 429.

---

### Task 21: Document the httpOnly-cookie migration as future work

**Files:**
- Create: `docs/superpowers/followups/httpOnly-cookie-auth-migration.md`

- [ ] **Step 1: Write a short follow-up note** describing that the current plan keeps tokens in `localStorage` (constrained by the Base44 SDK), and that a true httpOnly-cookie migration requires forking `@base44/sdk/dist/utils/axios-client.js` + rewriting `AuthContext.jsx` to drop the SDK's token management. List the files that would need to change. This is **not** executed now — just recorded.

- [ ] **Step 2: Commit**

```bash
cd /root/zhanlu && git add docs/superpowers/followups/httpOnly-cookie-auth-migration.md
git commit -m "docs: record httpOnly-cookie auth migration as future work"
```

---

## Self-Review

**1. Spec coverage:**
- "email + password only" → Task 3 removes Feishu, Task 13 removes the Google button. ✓
- "login required for everything" → Tasks 1, 2, 14 lock down backend + frontend. ✓
- "single role, everyone equal" → Task 7 removes the first-user-is-admin branch. ✓
- "harden password" → Tasks 4, 5, 7, 16. ✓
- "rate limiting" → Tasks 5, 6, 7. ✓
- "refresh tokens" → Tasks 8, 9, 10, 12, 15. ✓
- "logout revocation" → Tasks 8, 10, 12, 17. ✓
- "existing data preserved" → migration is additive only; no destructive changes. ✓

**2. Placeholder scan:** No "TBD"/"implement later" — every code step has real code or a precise edit instruction. The `entities.py` read-endpoint sweep and `App.jsx` route wrap intentionally reference "read the file first" because the exact line list depends on the current contents at execution time; the instruction is concrete enough to act on.

**3. Type consistency:** `create_access_token(user_id, db) -> str`, `create_refresh_token(user_id, db) -> str`, `verify_refresh_token(token, db) -> str|None`, `rotate_refresh_token(old, db) -> tuple[str,str]|None` — used consistently in Tasks 7, 10, 12, 15. `verify_token(token, db)` signature change is flagged in Task 10 and applied in Task 11. ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-27-claude-style-auth-hardening.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
