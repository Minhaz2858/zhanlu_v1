"""Read UserSetting overrides and merge them into LLM call parameters.

Does NOT import from routers/ — this lives in services/ so it stays safe
to import from anywhere without circular dependency risk.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import UserSetting

logger = logging.getLogger(__name__)

# Sensible guard rails for values read from the DB so a misconfiguration
# (e.g. max_tokens=0 or temperature=5.0) cannot break the LLM call.
_MAX_TOKENS_MIN = 1
_MAX_TOKENS_MAX = 131_072   # upper bound across current providers
_TEMPERATURE_MIN = 0.0
_TEMPERATURE_MAX = 2.0


def get_user_llm_overrides(db: Session, user_id: str) -> dict[str, Any]:
    """Return non-null LLM overrides from the user's settings row.

    Returns an empty dict if no row exists or all LLM fields are null.
    Never raises — callers treat an empty dict as "no override".

    The route layer is responsible for combining these with any per-request
    explicit values (per-request wins).
    """
    row = (
        db.query(UserSetting)
        .filter(UserSetting.created_by_id == user_id)
        .filter(UserSetting.is_deleted == False)
        .first()
    )
    if not row:
        return {}

    out: dict[str, Any] = {}

    if row.temperature is not None:
        t = float(row.temperature)
        if _TEMPERATURE_MIN <= t <= _TEMPERATURE_MAX:
            out["temperature"] = t
        else:
            logger.warning(
                "User %s temperature %.2f out of range [%.1f, %.1f], ignored",
                user_id, t, _TEMPERATURE_MIN, _TEMPERATURE_MAX,
            )

    if row.max_tokens is not None:
        mt = int(row.max_tokens)
        if _MAX_TOKENS_MIN <= mt <= _MAX_TOKENS_MAX:
            # Clamp against the configured hard cap so a value that exceeds
            # the endpoint's real max_model_len (e.g. self-hosted vLLM
            # capped at 16384 while the user saved 32768) can never produce
            # a provider HTTP 400.
            cap = getattr(settings, "LLM_MAX_TOKENS_HARD_CAP", None)
            if cap:
                cap = int(cap)
                if mt > cap:
                    logger.warning(
                        "User %s max_tokens %d exceeds hard cap %d, clamped",
                        user_id, mt, cap,
                    )
                    mt = cap
            out["max_tokens"] = mt
        else:
            logger.warning(
                "User %s max_tokens %d out of range [%d, %d], ignored",
                user_id, mt, _MAX_TOKENS_MIN, _MAX_TOKENS_MAX,
            )

    if row.fallback_model and isinstance(row.fallback_model, str):
        fb = row.fallback_model.strip()
        if fb:
            out["fallback_model"] = fb

    return out
