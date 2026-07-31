"""Password complexity validation — mirrors the frontend policy (plan 2026-07-27)."""
import re

from app.config import settings


def validate_password(password: str) -> list[str]:
    """Return a list of human-readable error strings. Empty list = valid.

    Policy (all configurable via env, see ``app/config.py``):
      * minimum length (default 10)
      * at least one letter (default on)
      * at least one digit (default on)
    """
    errors: list[str] = []
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        errors.append(
            f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters long."
        )
    if settings.PASSWORD_REQUIRE_LETTER and not re.search(r"[A-Za-z]", password):
        errors.append("Password must contain at least one letter.")
    if settings.PASSWORD_REQUIRE_DIGIT and not re.search(r"\d", password):
        errors.append("Password must contain at least one digit.")
    return errors
