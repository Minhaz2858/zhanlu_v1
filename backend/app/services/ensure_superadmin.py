"""Idempotent super-admin bootstrap — called on startup after system agents.

Reads ``SUPERADMIN_EMAIL`` and ``SUPERADMIN_PASSWORD`` from settings on every
startup.  If both are set and no user with that email exists: creates one with
role='admin'.  If the user already exists, makes sure the password is current
(updates it in place — covers password rotation on next restart).

If either env var is empty: no-op (backward compatible — existing deployments
that don't set these vars are completely unaffected).
"""

import logging

from app.database import SessionLocal
from app.models.user import User
from app.services.auth_service import auth_service

logger = logging.getLogger(__name__)


def ensure_superadmin(db=None) -> None:
    """Seed (or update) the single super-admin account from env vars.

    Idempotent — safe to call on every startup.  Wrapped in a try/except
    so a misconfiguration never blocks the server from starting.

    Args:
        db: Optional SQLAlchemy Session.  When None (startup path), creates its
            own session via SessionLocal.  Pass a session directly in tests.
    """
    from app.config import settings

    email = (settings.SUPERADMIN_EMAIL or "").strip()
    password = (settings.SUPERADMIN_PASSWORD or "").strip()

    if not email:
        logger.debug("ensure_superadmin: SUPERADMIN_EMAIL is empty — skipping")
        return
    if not password:
        logger.warning(
            "ensure_superadmin: SUPERADMIN_EMAIL is set but "
            "SUPERADMIN_PASSWORD is empty — skipping (password required)"
        )
        return

    close_on_exit = False
    if db is None:
        db = SessionLocal()
        close_on_exit = True

    try:
        existing = db.query(User).filter(
            User.email == email, User.is_deleted == False
        ).first()

        if existing:
            # Update password in-place (supports rotation via restart).
            existing.password_hash = auth_service.hash_password(password)
            if existing.role != "admin":
                existing.role = "admin"
                logger.info(
                    "ensure_superadmin: promoted existing user %r to admin", email
                )
            else:
                logger.info(
                    "ensure_superadmin: updated password for existing admin %r", email
                )
        else:
            user = User(
                email=email,
                full_name="Super Admin",
                role="admin",
                password_hash=auth_service.hash_password(password),
            )
            db.add(user)
            logger.info("ensure_superadmin: created super-admin account %r", email)

        db.commit()
    except Exception:
        logger.exception("ensure_superadmin: failed — server will still start")
        db.rollback()
    finally:
        if close_on_exit:
            db.close()
