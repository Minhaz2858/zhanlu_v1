"""Lightweight Fernet encryption / decryption for stored secrets.

Uses ``LLM_MODEL_ENCRYPTION_KEY`` from environment (or ``settings.LLM_CRYPTO_KEY``).
Falls back to plaintext with a loud WARNING for dev convenience.
"""

import logging
import os

logger = logging.getLogger(__name__)

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.getenv("LLM_MODEL_ENCRYPTION_KEY", "")
    if not key:
        logger.warning(
            "LLM_MODEL_ENCRYPTION_KEY is not set — llm_models.api_key will be "
            "stored/retrieved in PLAINTEXT. Set this env var for production."
        )
        _fernet = False  # sentinel: crypto unavailable
        return _fernet
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except Exception:
        logger.exception("Invalid Fernet key; api_key encryption is disabled.")
        _fernet = False
    return _fernet


def encrypt_value(plain: str | None) -> str | None:
    """Encrypt *plain*. If crypto is unavailable, return plaintext as-is."""
    if plain is None:
        return None
    fernet = _get_fernet()
    if fernet is False:
        return plain
    return fernet.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_value(cipher: str | None) -> str | None:
    """Decrypt *cipher*. If it is plaintext (crypto unavailable), return as-is."""
    if cipher is None:
        return None
    fernet = _get_fernet()
    if fernet is False:
        return cipher
    try:
        return fernet.decrypt(cipher.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.warning("Failed to decrypt api_key — returning None.")
        return None
