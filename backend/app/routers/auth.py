"""Auth router — login, register, OTP, password reset, User/me, logout redirect.

All endpoints match the Base44 SDK auth module paths:
  POST /apps/{app_id}/auth/login
  POST /apps/{app_id}/auth/register
  POST /apps/{app_id}/auth/verify-otp
  POST /apps/{app_id}/auth/resend-otp
  POST /apps/{app_id}/auth/reset-password-request
  POST /apps/{app_id}/auth/reset-password
  POST /apps/{app_id}/auth/change-password
  GET  /apps/{app_id}/entities/User/me   (special — not standard entity CRUD)
  PUT  /apps/{app_id}/entities/User/me
  GET  /apps/auth/logout                 (redirects to frontend)
"""

import logging
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db, get_current_user_required
from app.middleware.rate_limit import rate_limit
from app.models.user import User
from app.services.auth_service import auth_service
from app.services.mail_service import send_otp_email
from app.services.password_policy import validate_password

logger = logging.getLogger(__name__)


router = APIRouter(tags=["auth"])


@router.post("/apps/{app_id}/auth/login")
async def login(
    app_id: str,
    body: dict,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit(settings.RATE_LIMIT_LOGIN_PER_MIN, 60)),
):
    """Login with email and password. Returns {access_token, user}."""
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    user = db.query(User).filter(
        User.email == email, User.is_deleted == False
    ).first()

    if not user or not auth_service.verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = auth_service.create_access_token(user.id, db)
    refresh_token = auth_service.create_refresh_token(user.id, db)
    return {"access_token": access_token, "refresh_token": refresh_token, "user": user.to_dict()}


@router.post("/apps/{app_id}/auth/request-login-code")
async def request_login_code(
    app_id: str,
    body: dict,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit(settings.RATE_LIMIT_LOGIN_PER_MIN, 60)),
):
    """Request a passwordless login code emailed to the user's address.

    The code is only generated (and emailed) when the email belongs to a
    registered, non-deleted account. The response is ALWAYS the generic
    ``{"sent": true}`` so an attacker cannot enumerate which emails are
    registered. The OTP is never returned in the response — it is emailed
    (best-effort via SMTP) and logged server-side as a dev fallback.
    """
    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Email required")

    otp = auth_service.request_login_code(email, db)
    if otp:
        try:
            emailed = await send_otp_email(email, otp, purpose="log in to your account")
        except Exception as e:  # noqa: BLE001 — email is best-effort
            logger.error("request-login-code: send_otp_email raised: %s", e)
            emailed = False
        # Always log the code server-side as a fallback (and for dev without SMTP).
        logger.warning("Login code for %s: %s (emailed=%s)", email, otp, emailed)
    # Generic success regardless of whether the email exists.
    return {"sent": True}


@router.post("/apps/{app_id}/auth/login-with-code")
async def login_with_code(
    app_id: str,
    body: dict,
    db: Session = Depends(get_db),
):
    """Log in with an emailed login code (passwordless). Returns {access_token, refresh_token, user}."""
    email = body.get("email")
    code = body.get("code")

    if not email or not code:
        raise HTTPException(status_code=400, detail="Email and code required")

    result = auth_service.login_with_code(email, code, db)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired login code")
    return result


@router.post("/apps/{app_id}/auth/register")
async def register(
    app_id: str,
    body: dict,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit(settings.RATE_LIMIT_REGISTER_PER_10MIN, 600)),
):
    """Register a new user.

    Requires either:
    - A verified OTP code (email + otp_code), or
    - No OTP flow if no email verification is configured (first-time setup).

    Single-role model (plan 2026-07-27): every user is ``role="user"``.

    When ``ALLOW_PUBLIC_REGISTRATION`` is False (enterprise / SaaS model),
    open self-registration is blocked — accounts are provisioned only by an
    admin via ``/api/admin/users``. A fresh install with zero users is still
    permitted to bootstrap the first (admin) account.
    """
    if not settings.ALLOW_PUBLIC_REGISTRATION and db.query(User).count() > 0:
        raise HTTPException(
            status_code=403,
            detail="Public registration is disabled. Contact an administrator to request an account.",
        )

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

    # If an OTP was sent, verify it. If no OTP was provided, require it once
    # any users exist (first registration may proceed without OTP for setup).
    if otp_code:
        if not auth_service.verify_otp(email, otp_code, db):
            raise HTTPException(status_code=400, detail="Invalid or expired OTP code")
    elif db.query(User).count() > 0:
        raise HTTPException(
            status_code=400,
            detail="OTP verification required. Please request an OTP code first."
        )

    user = User(
        email=email,
        full_name=full_name,
        role="user",
        password_hash=auth_service.hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Issue a real (access, refresh) token pair (plan 2026-07-27).
    access_token = auth_service.create_access_token(user.id, db)
    refresh_token = auth_service.create_refresh_token(user.id, db)
    return {"access_token": access_token, "refresh_token": refresh_token, "user": user.to_dict()}


@router.post("/apps/{app_id}/auth/verify-otp")
async def verify_otp(app_id: str, body: dict, db: Session = Depends(get_db)):
    """Verify OTP code against the database."""
    email = body.get("email")
    otp_code = body.get("otp_code")

    if not email or not otp_code:
        raise HTTPException(status_code=400, detail="Email and otp_code required")

    if not auth_service.verify_otp(email, otp_code, db):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code")

    return {"verified": True}


@router.post("/apps/{app_id}/auth/resend-otp")
async def resend_otp(app_id: str, body: dict, db: Session = Depends(get_db)):
    """Generate a new OTP code and email it to the user.

    Sends the code via the configured SMTP service (see app/services/mail_service).
    The OTP is NEVER returned in the API response — that would let anyone who
    knows an email address bypass verification. When SMTP is not configured,
    the code is logged at WARNING level (dev fallback) so local dev still works.
    """
    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Email required")

    otp = auth_service.generate_otp(email, db)

    # Actually email the code when SMTP is configured; otherwise log (dev).
    try:
        from app.services.mail_service import send_otp_email
        emailed = await send_otp_email(email, otp, purpose="verify your account")
    except Exception as e:  # noqa: BLE001 — email is best-effort
        logger.error("resend-otp: send_otp_email raised: %s", e)
        emailed = False

    # Always log the code server-side as a fallback (and for dev without SMTP).
    logger.warning("OTP for %s: %s (emailed=%s)", email, otp, emailed)
    return {"sent": True, "emailed": emailed}


@router.post("/apps/{app_id}/auth/reset-password-request")
async def reset_password_request(app_id: str, body: dict, db: Session = Depends(get_db)):
    """Request a password reset. Creates a secure reset token if user exists
    and emails a reset link to the user (when SMTP is configured)."""
    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Email required")

    user = db.query(User).filter(User.email == email, User.is_deleted == False).first()
    if user:
        token = auth_service.create_reset_token(user.id, db)
        # Email the reset link (best-effort). The link points at the frontend's
        # reset page with the token pre-filled. Falls back to logging the token.
        try:
            from app.services.mail_service import send_email
            reset_url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?reset_token={token}"
            html = f"""
            <div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:24px">
              <h2 style="color:#b45309;margin:0 0 8px">Zhanlu System · Synexia</h2>
              <p style="color:#444;font-size:15px;line-height:1.6">
                Reset your password by clicking the button below (valid for 1 hour).
              </p>
              <p style="margin:24px 0;text-align:center">
                <a href="{reset_url}" style="background:#b45309;color:#fff;text-decoration:none;
                   padding:12px 24px;border-radius:8px;display:inline-block">Reset password</a>
              </p>
              <p style="color:#888;font-size:12px">Or paste this token: <code>{token}</code></p>
            </div>
            """
            await send_email(email, "Reset your Zhanlu password", html, f"Reset your password: {reset_url}")
        except Exception as e:  # noqa: BLE001
            logger.error("reset-password-request: send_email raised: %s", e)
        logger.warning("Password reset token for %s: %s (emailed if SMTP configured)", email, token)

    # Always return success to prevent email enumeration
    return {"sent": True}


@router.post("/apps/{app_id}/auth/reset-password")
async def reset_password(app_id: str, body: dict, db: Session = Depends(get_db)):
    """Reset password using a secure reset token."""
    reset_token = body.get("reset_token")
    new_password = body.get("new_password")

    if not reset_token or not new_password:
        raise HTTPException(status_code=400, detail="reset_token and new_password required")

    pw_errors = validate_password(new_password)
    if pw_errors:
        raise HTTPException(status_code=400, detail="; ".join(pw_errors))

    user_id = auth_service.verify_reset_token(reset_token, db)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user = db.query(User).filter(User.id == user_id, User.is_deleted == False).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password_hash = auth_service.hash_password(new_password)
    db.commit()
    # Invalidate all existing refresh tokens so the reset also logs out any
    # active sessions (defense-in-depth if the old password was compromised).
    auth_service.revoke_all_user_refresh_tokens(user.id, db)
    auth_service.consume_reset_token(reset_token, db)
    return {"success": True}


@router.post("/apps/{app_id}/auth/change-password")
async def change_password(
    app_id: str,
    body: dict,
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Change password for the authenticated user.

    The user identity is taken from the JWT token, NOT from the request
    body, so an attacker cannot change another user's password even if
    they know the current password.
    """
    current_password = body.get("current_password")
    new_password = body.get("new_password")

    if not current_password or not new_password:
        raise HTTPException(status_code=400, detail="current_password and new_password required")

    pw_errors = validate_password(new_password)
    if pw_errors:
        raise HTTPException(status_code=400, detail="; ".join(pw_errors))

    if not auth_service.verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    user.password_hash = auth_service.hash_password(new_password)
    db.commit()
    return {"success": True}


# --- Refresh / logout (plan 2026-07-27) ---

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
async def logout_revoke(
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
        exp_dt = datetime.fromtimestamp(payload.get("exp", 0), tz=timezone.utc)
        auth_service.revoke_access_token(jti, user.id, exp_dt, db)
    auth_service.revoke_all_user_refresh_tokens(user.id, db)
    return {"success": True}


# --- User/me endpoints (special — not standard entity CRUD) ---

@router.get("/apps/{app_id}/entities/User/me")
async def get_me(app_id: str, user: User = Depends(get_current_user_required)):
    """Get the current authenticated user."""
    return user.to_dict()


@router.put("/apps/{app_id}/entities/User/me")
async def update_me(
    app_id: str,
    body: dict,
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Update the current authenticated user."""
    valid_fields = {"email", "full_name"}
    for key, value in body.items():
        if key in valid_fields:
            setattr(user, key, value)

    from datetime import datetime
    user.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user.to_dict()


# --- Logout redirect ---

@router.get("/apps/auth/logout")
async def logout(request: Request):
    """Redirect to the frontend login page after logout.

    The SDK's logout() method redirects the browser here with a ``from_url``
    query param (typically a path like ``/``). We always send the browser to
    the frontend login page, passing from_url as the ``next`` param so
    ProtectedRoute can return the user to where they were after login.
    """
    from_url = request.query_params.get("from_url", "/")
    login_url = (
        f"{settings.FRONTEND_URL.rstrip('/')}/login"
        f"?next={from_url}"
    )
    return RedirectResponse(url=login_url, status_code=302)
