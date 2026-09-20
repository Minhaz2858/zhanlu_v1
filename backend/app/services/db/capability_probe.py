"""Discover a source's capability profile from the source itself.

Runs once per catalog index (and on demand), against the SAME connector the
query tools use, and persists what it actually observed into
``kb_capability_profiles``.

Two rules this module never breaks:

1. **Nothing is assumed from ``db_type`` alone.** The dialect layer supplies the
   probe statements; the probe records whether each one actually worked. A
   capability the source refused is recorded as ``False``/unknown with the
   error text — never as a default. (The one field that IS static is the
   dialect's own syntax map, because that is a property of the dialect, not of
   the connection.)
2. **A probe failure is never an index failure.** Every statement is guarded;
   the profile is written with whatever was learned, plus
   ``probe_evidence`` saying what was not.

Probe coverage (dialect permitting, from DialectProfile):

    version        server version string          → server_version
    current_schema / schemas                      → schemas[]
    timezone, locale                              → timezone, locale
    views                                         → supports_views, view_count
    window functions (live statement)             → supports_window_functions
    read-only account (grants / privileges)       → read_only
    row-count statistics                          → row_count_stats availability
    catalog digest                                → catalog_version
    connect+probe wall time                       → connect_latency_ms

No knowledge base id, table name, business term or vendor module is hardcoded
here: a newly connected database is profiled with zero code changes, and a
dialect the platform does not know yields ``UnknownDialect`` (recorded as
``detection_error``) rather than another vendor's behaviour.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.db.dialect import (
    DialectProfile,
    UnknownDialect,
    maybe_profile,
)

logger = logging.getLogger(__name__)

# Caps so a pathological source cannot blow up the index job.
_MAX_SCHEMAS = 50
_MAX_EVIDENCE_TEXT = 240

_WRITE_PRIVILEGES = ("insert", "update", "delete", "create", "drop", "alter", "all privileges")


def _scalar(rows: Any) -> Any:
    """First value of the first row of a connector result (list[dict])."""
    if not rows:
        return None
    first = rows[0]
    if isinstance(first, dict):
        for value in first.values():
            return value
        return None
    if isinstance(first, (list, tuple)) and first:
        return first[0]
    return first


def _run(conn: Any, sql: str, evidence: dict, key: str, *, max_rows: int = 100,
         timeout_s: int = 8) -> Any:
    """Execute one probe, recording success/failure into ``evidence``."""
    try:
        rows = conn.execute(sql, max_rows=max_rows, timeout_s=timeout_s)
        evidence[key] = {"ok": True, "sql": sql, "value": _truncate(_scalar(rows))}
        return rows
    except Exception as exc:  # noqa: BLE001 — a probe must never break the index
        evidence[key] = {"ok": False, "sql": sql, "error": _truncate(str(exc))}
        logger.debug("capability_probe: %s probe failed: %s", key, exc)
        return None


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_EVIDENCE_TEXT:
        return value[:_MAX_EVIDENCE_TEXT] + "…"
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _parse_read_only(profile: DialectProfile, rows: Any) -> bool | None:
    """Interpret a read-only probe result. None = could not determine.

    ``True`` means the account cannot modify the source's data (or the session
    is in read-only mode). ``False`` means it can. Unknown stays unknown — the
    profile records ``None`` rather than guessing.
    """
    if not rows:
        return None
    name = profile.name
    if name == "mysql":
        # SHOW GRANTS FOR CURRENT_USER() → one row per grant line.
        grants = " ".join(
            str(v).lower() for row in rows for v in (row.values() if isinstance(row, dict) else row)
        )
        if not grants.strip():
            return None
        return not any(p in grants for p in _WRITE_PRIVILEGES)
    if name in ("postgres", "mssql"):
        value = _scalar(rows)
        if value is None:
            return None
        try:
            return not bool(value)
        except Exception:
            return None
    if name == "sqlite":
        value = _scalar(rows)
        if value is None:
            return None
        try:
            return bool(int(value))
        except Exception:
            return None
    return None


def _catalog_version(db: Session, kb_id: str, tables: list[dict] | None) -> str | None:
    """Digest of the catalog this profile was validated against.

    Prefers the shared schema-shape digest (same one the change detector uses);
    falls back to a digest of the discovered table names so a source still gets
    a version even when the shape probe is unavailable.
    """
    try:
        from app.services.db.schema_shape import catalog_digest

        digest = catalog_digest(db, kb_id)
        if digest:
            return f"shape:{digest}"
    except Exception:
        logger.debug("capability_probe: shape digest unavailable", exc_info=True)
    if tables:
        names = sorted(str(t.get("table_name") or "") for t in tables)
        blob = json.dumps(names, separators=(",", ":"), ensure_ascii=False)
        return "tables:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
    return None


def probe_source(
    kb: Any,
    db: Session,
    *,
    tables: list[dict] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Collect the capability profile for one source. Never raises.

    Returns a dict shaped like ``KBCapabilityProfile`` columns. When ``conn`` is
    supplied it is reused (the indexer already holds one); otherwise a connector
    is opened via the factory.
    """
    from app.config import settings

    db_type = getattr(kb, "db_type", None)
    profile = maybe_profile(db_type)
    if profile is None:
        return {
            "kb_id": getattr(kb, "id", None),
            "dialect": "",
            "db_type_raw": db_type or "",
            "detection_error": (
                f"No dialect capability profile for db_type={db_type!r} — "
                "refusing to assume another vendor's syntax."
            ),
            "probe_evidence": {"dialect": {"ok": False, "value": db_type or ""}},
        }

    evidence: dict[str, Any] = {}
    started = time.monotonic()
    caps = profile.capability_dict()

    schemas: list[str] = []
    server_version = None
    timezone_name = None
    locale_name = None
    view_count: int | None = None
    supports_views = profile.supports_views
    supports_window = profile.supports_window_functions
    read_only: bool | None = None
    row_count_stats = bool(profile.row_count_sql)
    connect_ms: float | None = None

    own_conn = conn is None
    opened = None
    try:
        if own_conn:
            from app.services.db.connector_factory import get_connector

            opened = get_connector(kb)
            conn = opened.__enter__()
        connect_ms = round((time.monotonic() - started) * 1000, 1)

        # ── identity ──────────────────────────────────────────────────────
        if profile.version_sql:
            server_version = _scalar(_run(conn, profile.version_sql, evidence, "version"))
            if isinstance(server_version, str):
                server_version = server_version.strip().splitlines()[0][:255]

        if profile.current_schema_sql:
            _run(conn, profile.current_schema_sql, evidence, "current_schema")

        # ── namespaces ────────────────────────────────────────────────────
        if profile.list_schemas_sql:
            rows = _run(
                conn, profile.list_schemas_sql, evidence, "schemas",
                max_rows=_MAX_SCHEMAS,
            )
            schemas = _schemas_from(rows)

        # ── clock / locale ────────────────────────────────────────────────
        if profile.timezone_sql:
            value = _scalar(_run(conn, profile.timezone_sql, evidence, "timezone"))
            timezone_name = str(value)[:64] if value is not None else None
        if profile.locale_sql:
            value = _scalar(_run(conn, profile.locale_sql, evidence, "locale"))
            locale_name = str(value)[:64] if value is not None else None

        # ── views ─────────────────────────────────────────────────────────
        if profile.views_sql:
            rows = _run(conn, profile.views_sql, evidence, "views", max_rows=1)
            value = _scalar(rows)
            if value is not None:
                try:
                    view_count = int(value)
                    supports_views = True
                except (TypeError, ValueError):
                    supports_views = False

        # ── window functions (live statement, not a version guess) ────────
        if profile.window_probe_sql:
            rows = _run(conn, profile.window_probe_sql, evidence, "window_functions", max_rows=1)
            if rows is None:
                supports_window = False
            else:
                supports_window = True

        # ── read-only account ─────────────────────────────────────────────
        if profile.read_only_sql:
            rows = _run(conn, profile.read_only_sql, evidence, "read_only", max_rows=20)
            read_only = _parse_read_only(profile, rows)

    except UnknownDialect as exc:  # pragma: no cover - guarded above
        evidence["dialect"] = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        evidence["connection"] = {"ok": False, "error": _truncate(str(exc))}
        logger.warning("capability_probe: source %s probe failed: %s", getattr(kb, "id", "?"), exc)
    finally:
        if own_conn and opened is not None:
            try:
                opened.__exit__(None, None, None)
            except Exception:
                logger.debug("capability_probe: connector close failed", exc_info=True)

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    return {
        "kb_id": getattr(kb, "id", None),
        "dialect": profile.name,
        "db_type_raw": (db_type or "")[:64],
        "server_version": server_version,
        "quote_style": profile.quote_style,
        "quote_open": profile.quote_open,
        "quote_close": profile.quote_close,
        "pagination": profile.pagination,
        "null_function": profile.null_function,
        "concat_template": profile.concat_template,
        "supports_views": bool(supports_views),
        "supports_window_functions": bool(supports_window),
        "supports_explain": bool(profile.explain_prefix),
        "read_only": read_only,
        "catalogs": [],
        "schemas": schemas,
        "timezone": timezone_name,
        "locale": locale_name,
        "query_timeout_s": int(getattr(settings, "MAX_QUERY_TIMEOUT_S", 30)),
        "max_rows": int(getattr(settings, "MAX_QUERY_ROWS_CAP", 5000)),
        "connect_latency_ms": connect_ms if connect_ms is not None else elapsed_ms,
        "capabilities": {
            **caps,
            "view_count": view_count,
            "row_count_stats": row_count_stats,
            "probe_duration_ms": elapsed_ms,
        },
        "probe_evidence": evidence,
        "catalog_version": _catalog_version(db, getattr(kb, "id", ""), tables),
        "table_count": len(tables) if tables is not None else None,
        "detected_at": datetime.utcnow(),
        "detection_error": None,
    }


def _schemas_from(rows: Any) -> list[str]:
    """Flatten a schema-listing result into a bounded list of names."""
    if not rows:
        return []
    out: list[str] = []
    for row in rows:
        values = row.values() if isinstance(row, dict) else row
        for value in values:
            if value is None:
                continue
            name = str(value).strip()
            if name and name not in out:
                out.append(name[:128])
            break
    return out[:_MAX_SCHEMAS]


def store_profile(db: Session, kb: Any, payload: dict[str, Any], *, commit: bool = True) -> Any:
    """Upsert a probe payload into ``kb_capability_profiles``. Best-effort."""
    if not payload or not payload.get("kb_id"):
        return None
    try:
        from app.models.kb_capability import KBCapabilityProfile

        row = (
            db.query(KBCapabilityProfile)
            .filter(KBCapabilityProfile.kb_id == payload["kb_id"])
            .first()
        )
        if row is None:
            row = KBCapabilityProfile(kb_id=payload["kb_id"])
            db.add(row)
        for field, value in payload.items():
            if field == "kb_id" or not hasattr(row, field):
                continue
            setattr(row, field, value)
        if commit:
            db.commit()
        return row
    except Exception:
        logger.exception("capability_probe: could not persist profile for %s", payload.get("kb_id"))
        try:
            db.rollback()
        except Exception:
            pass
        return None


def detect_and_store(
    db: Session, kb: Any, *, tables: list[dict] | None = None, conn: Any = None
) -> dict[str, Any] | None:
    """Probe + persist in one call. Returns the stored row (or None)."""
    from app.config import settings

    if not getattr(settings, "CAPABILITY_PROBE_ENABLED", True):
        logger.info("capability_probe: disabled by CAPABILITY_PROBE_ENABLED")
        return None
    payload = probe_source(kb, db, tables=tables, conn=conn)
    row = store_profile(db, kb, payload)
    if payload.get("detection_error"):
        logger.warning(
            "capability_probe: source %s has no usable dialect profile: %s",
            payload.get("kb_id"), payload["detection_error"],
        )
    return row


def cap_dialect(db: Session, kb_id: str) -> str | None:
    """Persisted dialect for a source, or None when no profile exists.

    The selection order answering paths must use: persisted capability profile
    first (it is the source's own recorded truth), then nothing — never a
    literal default.
    """
    try:
        from app.models.kb_capability import KBCapabilityProfile

        row = (
            db.query(KBCapabilityProfile.dialect)
            .filter(
                KBCapabilityProfile.kb_id == kb_id,
                KBCapabilityProfile.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        # Only a real string is a dialect. A mock/deferred query object (or a
        # column that came back as a non-string) must never be coerced into a
        # dialect name — that would be a default dialect wearing a disguise.
        value = row[0] if row else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        logger.debug("capability_probe: cap_dialect lookup failed", exc_info=True)
    return None


def profile_for_kb(db: Session, kb: Any) -> DialectProfile | None:
    """Dialect profile for a KnowledgeBase row.

    Order: persisted capability profile → the source's own ``db_type`` →
    None. A literal default dialect is never returned (that was the
    ``or "mysql"`` bug this replaces).
    """
    kb_id = getattr(kb, "id", None) if not isinstance(kb, dict) else kb.get("id")
    if kb_id:
        persisted = cap_dialect(db, kb_id)
        if persisted:
            prof = maybe_profile(persisted)
            if prof is not None:
                return prof
    db_type = kb.get("db_type") if isinstance(kb, dict) else getattr(kb, "db_type", None)
    return maybe_profile(db_type)


def check_readonly_guard(db: Session, kb_id: str) -> bool:
    """True when the source is known to be read-only by capability.

    Used by execution paths to decide whether a write statement should be
    refused by capability (in addition to the structural read-only gate).
    Unknown (no profile) returns False — the structural gate still applies.
    """
    try:
        from app.models.kb_capability import KBCapabilityProfile

        row = (
            db.query(KBCapabilityProfile.read_only)
            .filter(
                KBCapabilityProfile.kb_id == kb_id,
                KBCapabilityProfile.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        value = row[0] if row else None
        return bool(value) if isinstance(value, bool) else False
    except Exception:
        return False
