"""workspace_settings_service — typed read/write for ``WorkspaceSetting`` rows.

Hot path (``get_bool``, ``get_int``, ``get_str``) is in-process memoized
with a 5-second TTL so a tight agent loop doesn't hammer the DB. The
writer (``set``) invalidates the cache for that key so the next read
sees the new value.

The org/app scope matches the rest of the system (``TimestampedBase``
defaults to ``"default-org"`` / ``"default-app"``). Callers that need a
different scope can pass ``org_id`` / ``app_id`` explicitly.

This service is intentionally tiny — the storage layer is just a table
with a few indexes (see migration 010).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.workspace_settings import WorkspaceSetting

logger = logging.getLogger(__name__)


# Key constants — keep them in one place so callers don't drift.
KEY_AUTO_BIND_ALL_DATASOURCES = "auto_bind_all_datasources"

# Default values for known flags. Used when no row exists.
_DEFAULTS = {
    KEY_AUTO_BIND_ALL_DATASOURCES: False,
}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


# Keyed by (org_id, app_id, key) → (value, expires_at_monotonic)
_cache: dict[tuple[str, str, str], tuple[Any, float]] = {}
_CACHE_TTL_SECONDS = 5.0


def _scope_key(org_id: str, app_id: str, key: str) -> tuple[str, str, str]:
    return (org_id, app_id, key)


def _cache_get(org_id: str, app_id: str, key: str) -> Optional[Any]:
    entry = _cache.get(_scope_key(org_id, app_id, key))
    if not entry:
        return None
    value, expires = entry
    if time.monotonic() > expires:
        _cache.pop(_scope_key(org_id, app_id, key), None)
        return None
    return value


def _cache_set(org_id: str, app_id: str, key: str, value: Any) -> None:
    _cache[_scope_key(org_id, app_id, key)] = (
        value,
        time.monotonic() + _CACHE_TTL_SECONDS,
    )


def _cache_invalidate(org_id: str, app_id: str, key: str) -> None:
    _cache.pop(_scope_key(org_id, app_id, key), None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_bool(
    db: Session,
    key: str,
    *,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> bool:
    """Return the boolean value of a workspace setting, or its default.

    Falsy representations: empty string, ``"false"``, ``"0"``, ``"no"``
    (case-insensitive). Everything else is truthy.
    """
    cached = _cache_get(org_id, app_id, key)
    if cached is not None:
        return bool(cached)
    raw = _read_raw(db, key, org_id=org_id, app_id=app_id)
    if raw is None:
        value = _DEFAULTS.get(key, False)
    else:
        value = raw.strip().lower() not in ("", "false", "0", "no", "off")
    _cache_set(org_id, app_id, key, value)
    return bool(value)


def get_str(
    db: Session,
    key: str,
    *,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> Optional[str]:
    """Return the raw string value, or None if not set."""
    return _read_raw(db, key, org_id=org_id, app_id=app_id)


def get_int(
    db: Session,
    key: str,
    *,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> Optional[int]:
    """Return the int value, or None if not set or not parseable."""
    raw = _read_raw(db, key, org_id=org_id, app_id=app_id)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("workspace_settings[%s] is not an int: %r", key, raw)
        return None


def set_value(
    db: Session,
    key: str,
    value: str,
    *,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> WorkspaceSetting:
    """Upsert a setting, invalidate its cache entry, return the row."""
    row = (
        db.query(WorkspaceSetting)
        .filter(
            WorkspaceSetting.org_id == org_id,
            WorkspaceSetting.app_id == app_id,
            WorkspaceSetting.key == key,
            WorkspaceSetting.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        row = WorkspaceSetting(
            org_id=org_id,
            app_id=app_id,
            key=key,
            value=value,
        )
        db.add(row)
    else:
        row.value = value
    db.flush()
    _cache_invalidate(org_id, app_id, key)
    return row


def clear_cache() -> None:
    """Drop every cached entry. Useful for tests."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _read_raw(
    db: Session,
    key: str,
    *,
    org_id: str,
    app_id: str,
) -> Optional[str]:
    row = (
        db.query(WorkspaceSetting)
        .filter(
            WorkspaceSetting.org_id == org_id,
            WorkspaceSetting.app_id == app_id,
            WorkspaceSetting.key == key,
            WorkspaceSetting.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        return None
    return row.value
