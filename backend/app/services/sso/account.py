"""SSO account linking — find-or-create a User by verified IdP email."""

from __future__ import annotations

import secrets
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import User


def is_email_allowed(email: str) -> bool:
    """True when the email's domain passes ``SSO_ALLOWED_EMAIL_DOMAINS``.

    Empty allowlist ⇒ allow all. A domain entry matches the domain itself
    and any subdomain (``example.com`` matches ``alice@example.com`` and
    ``alice@sub.example.com``).
    """
    allowlist = settings.SSO_ALLOWED_EMAIL_DOMAINS or []
    if not allowlist:
        return True
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.split("@")[-1].strip().rstrip(".")
    if not domain:
        return False
    for entry in allowlist:
        d = (entry or "").strip().lower().lstrip(".").rstrip(".")
        if d and (domain == d or domain.endswith("." + d)):
            return True
    return False


def find_or_create_sso_user(
    db: Session,
    *,
    email: str,
    full_name: str,
    provider_id: str,
) -> Optional[User]:
    """Link an IdP user to a local account.

    - Domain not allowed ⇒ None (no account created).
    - Existing (non-deleted) user with the same email ⇒ returned (linked).
    - Otherwise creates a user with an unusable random password hash
      (the account can only be signed into via SSO).
    """
    email = (email or "").strip().lower()
    if not email or not is_email_allowed(email):
        return None
    existing = db.query(User).filter(
        User.email == email, User.is_deleted == False  # noqa: E712
    ).first()
    if existing:
        return existing
    user = User(
        email=email,
        full_name=(full_name or email.split("@")[0])[:255],
        # Unusable hash — SSO-only accounts never authenticate by password.
        password_hash="!" + secrets.token_urlsafe(32),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


__all__ = ["is_email_allowed", "find_or_create_sso_user"]
