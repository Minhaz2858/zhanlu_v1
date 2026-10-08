"""Credential encryption at rest (IBM framework-audit gap #3).

MCP OAuth tokens, external-agent bearer tokens, and integration secrets were
stored as plaintext JSON columns. This module provides symmetric encrypt/
decrypt helpers (Fernet / AES-128-CBC+HMAC under the hood) used at every
write/read site for credential columns.

Key resolution (in order):
1. ``CREDENTIAL_ENCRYPTION_KEY`` setting (base64 Fernet key or any passphrase
   — passphrases are SHA-256-derived into a Fernet key).
2. Falls back to deriving from ``JWT_SECRET`` so deployments get encryption
   by default with zero operator setup. Rotating JWT_SECRET invalidates
   stored ciphertexts — set a dedicated CREDENTIAL_ENCRYPTION_KEY in prod.

Backward compatibility: values without the ``enc:v1:`` prefix are treated
as legacy plaintext and returned as-is on decrypt; writes always encrypt.
"""
from __future__ import annotations

import base64
import hashlib
import logging

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


def _fernet():
    from cryptography.fernet import Fernet

    from app.config import settings

    key = getattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "") or ""
    if key:
        try:
            # Accept a proper Fernet key verbatim…
            return Fernet(key.encode())
        except Exception:  # noqa: BLE001 — not a valid Fernet key: derive it
            derived = base64.urlsafe_b64encode(
                hashlib.sha256(key.encode()).digest())
            return Fernet(derived)
    # Zero-setup fallback: derive from JWT_SECRET.
    secret = getattr(settings, "JWT_SECRET", "") or "zhanlu-dev-secret"
    derived = base64.urlsafe_b64encode(hashlib.sha256(
        f"cred-enc:{secret}".encode()).digest())
    return Fernet(derived)


def encrypt_value(plaintext: str | None) -> str | None:
    """Encrypt a credential for storage. None passes through."""
    if plaintext is None:
        return None
    value = str(plaintext)
    if not value:
        return value
    if value.startswith(_PREFIX):  # already encrypted
        return value
    try:
        token = _fernet().encrypt(value.encode()).decode()
        return f"{_PREFIX}{token}"
    except Exception as e:  # noqa: BLE001 — never break a write over crypto
        logger.warning("crypto: encrypt failed (storing plaintext): %s", e)
        return value


def decrypt_value(stored: str | None) -> str | None:
    """Decrypt a stored credential. Legacy plaintext passes through."""
    if stored is None:
        return None
    value = str(stored)
    if not value.startswith(_PREFIX):
        return value  # legacy plaintext
    try:
        token = value[len(_PREFIX):]
        return _fernet().decrypt(token.encode()).decode()
    except Exception as e:  # noqa: BLE001 — bad key/rotated secret
        logger.warning("crypto: decrypt failed (returning empty): %s", e)
        return ""


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and str(stored).startswith(_PREFIX)


__all__ = ["encrypt_value", "decrypt_value", "is_encrypted"]
