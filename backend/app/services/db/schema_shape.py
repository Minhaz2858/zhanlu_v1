"""Schema-shape signatures — noticing that a datasource's SHAPE changed.

Why this exists (G4). The live-schema cache was keyed by CONNECTION IDENTITY
(``connection_fingerprint``: db_type/host/port/database/schema). That is the correct
key for "this KB was re-pointed at a DIFFERENT database" and the wrong key for "the
database I am already pointing at changed". A column added to a live warehouse went
unseen for ``SCHEMA_CACHE_TTL_SECONDS`` (1 h default), and the catalog stayed stale
until a human clicked re-index — with no signal on either side.

A SHAPE signature is the missing half: a digest over the live (table, column) name set.
Two consumers:

* every schema cache adds it to its key, so a shape change is an automatic cache miss
  and the next call re-introspects — no restart, no 1 h wait;
* the CATALOG's own shape (derived from ``kb_table_meta`` / ``kb_column_meta``) is
  compared against the live one to report catalog drift: the tables and columns the
  business layer does not know about yet.

Cost, measured on the live 143-table / 2,643-column warehouse: ONE dialect query,
~0.9-1.1 s — the same order as the existing ``list_tables()`` round trip (1.0 s) and
~25x cheaper than re-introspecting every table (~24 s). SQLAlchemy's
``Inspector.get_multi_columns()`` is NOT usable there: on SQLAlchemy 2.0.36 it is not a
bulk query for MySQL/MariaDB (6.3-7.3 s) and returned 1,886 of 2,643 columns.

Names are case-folded: MySQL reports a column in the case it was created in, and a
casing difference is not a schema change. Types are deliberately NOT part of the
digest — the catalog stores the raw ``data_type`` ('varchar') while a driver renders
its own ('VARCHAR(50)'), so hashing types makes every KB look drifted.

Read-only: this module performs no writes to the datasource and none to the platform DB.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Bound the drift detail carried into logs/results. A warehouse that gained 2,000
# columns must not push 2,000 identifiers into a prompt.
DEFAULT_DRIFT_CAP = 20

# Short TTL for the shape probe itself. It is deliberately much shorter than
# SCHEMA_CACHE_TTL_SECONDS: the whole point is to notice drift sooner than an hour,
# while keeping the probe off the per-call path.
_DEFAULT_SHAPE_TTL = 300

# (kb_id, connection_fingerprint) -> (monotonic_ts, digest, pairs)
_SHAPE_CACHE: dict[tuple[str, str], tuple[float, str, tuple[tuple[str, str], ...]]] = {}
# kb_id -> digest last observed. A change here is what triggers invalidation.
_LAST_DIGEST: dict[str, str] = {}
_SHAPE_LOCK = threading.Lock()


def _settings() -> Any:
    from app.config import settings

    return settings


# ── pure helpers ───────────────────────────────────────────────────────────
def normalize_pairs(rows: Iterable[Sequence[Any]]) -> tuple[tuple[str, str], ...]:
    """Case-fold and de-duplicate (table, column) rows into a sorted tuple.

    Sorted and de-duplicated so the digest is independent of result order — MySQL
    and Postgres both order information_schema differently, and nothing guarantees
    ordinal ordering across dialects.
    """
    return tuple(
        sorted(
            {
                (str(t or "").strip().lower(), str(c or "").strip().lower())
                for t, c in (tuple(r)[:2] for r in rows)
                if t or c
            }
        )
    )


def shape_digest(pairs: Iterable[Sequence[str]]) -> str:
    """16-hex digest of a normalised (table, column) set. '' for an empty set."""
    pairs = tuple(pairs)
    if not pairs:
        return ""
    body = "\n".join(f"{t}.{c}" for t, c in pairs)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ShapeState:
    """One KB's live shape, as observed (or remembered) just now."""

    digest: str = ""
    pairs: tuple[tuple[str, str], ...] = ()
    changed: bool = False  # differs from the previous observation for this KB
    previous: str = ""     # the digest it changed from ("" when unknown)
    from_cache: bool = False
    error: str = ""

    @property
    def known(self) -> bool:
        return bool(self.digest)


@dataclass
class DriftReport:
    """What the live datasource has that the catalog does not know about."""

    live_digest: str = ""
    catalog_digest: str = ""
    tables_added: list[str] = field(default_factory=list)
    tables_dropped: list[str] = field(default_factory=list)
    columns_added: list[str] = field(default_factory=list)
    columns_dropped: list[str] = field(default_factory=list)
    live_tables: int = 0
    live_columns: int = 0
    catalog_tables: int = 0
    catalog_columns: int = 0

    @property
    def drifted(self) -> bool:
        return bool(
            self.tables_added
            or self.tables_dropped
            or self.columns_added
            or self.columns_dropped
        )

    def as_dict(self, cap: int = DEFAULT_DRIFT_CAP) -> dict[str, Any]:
        def bounded(items: list[str]) -> list[str]:
            return items[:cap]

        out: dict[str, Any] = {
            "drifted": self.drifted,
            "live_tables": self.live_tables,
            "live_columns": self.live_columns,
            "catalog_tables": self.catalog_tables,
            "catalog_columns": self.catalog_columns,
            "tables_added": bounded(self.tables_added),
            "tables_dropped": bounded(self.tables_dropped),
            "columns_added": bounded(self.columns_added),
            "columns_dropped": bounded(self.columns_dropped),
            "counts": {
                "tables_added": len(self.tables_added),
                "tables_dropped": len(self.tables_dropped),
                "columns_added": len(self.columns_added),
                "columns_dropped": len(self.columns_dropped),
            },
            "truncated": any(
                len(x) > cap
                for x in (self.tables_added, self.tables_dropped,
                          self.columns_added, self.columns_dropped)
            ),
        }
        if self.drifted:
            out["hint"] = (
                "The datasource changed since this source was last indexed. The new "
                "items are queryable now; the catalog's business meaning (descriptions, "
                "domains, curated vocabulary) does not cover them until a re-index."
            )
        return out


def diff_pairs(
    live: Iterable[Sequence[str]], catalog: Iterable[Sequence[str]]
) -> DriftReport:
    """Compare a live (table, column) set against the catalog's, case-folded."""
    live_set = {(str(t).lower(), str(c).lower()) for t, c in live}
    cat_set = {(str(t).lower(), str(c).lower()) for t, c in catalog}
    live_tables = {t for t, _ in live_set}
    cat_tables = {t for t, _ in cat_set}
    return DriftReport(
        live_digest=shape_digest(live_set),
        catalog_digest=shape_digest(cat_set),
        tables_added=sorted(live_tables - cat_tables),
        tables_dropped=sorted(cat_tables - live_tables),
        columns_added=sorted(f"{t}.{c}" for t, c in live_set - cat_set),
        columns_dropped=sorted(f"{t}.{c}" for t, c in cat_set - live_set),
        live_tables=len(live_tables),
        live_columns=len(live_set),
        catalog_tables=len(cat_tables),
        catalog_columns=len(cat_set),
    )


# ── live probe ─────────────────────────────────────────────────────────────
def _fingerprint(db: Session, kb_id: str) -> str:
    """Connection identity for the probe cache key (best-effort '')."""
    try:
        from app.services.db.schema_service import connection_fingerprint

        return connection_fingerprint(db, kb_id)
    except Exception:  # pragma: no cover - defensive
        return ""


def invalidate_shape_cache(kb_id: str | None = None) -> None:
    """Drop remembered shapes — one KB, or all. Tests and re-pointing use this."""
    with _SHAPE_LOCK:
        if kb_id is None:
            _SHAPE_CACHE.clear()
            _LAST_DIGEST.clear()
            return
        for key in [k for k in _SHAPE_CACHE if k[0] == kb_id]:
            _SHAPE_CACHE.pop(key, None)
        _LAST_DIGEST.pop(kb_id, None)


def _probe_ttl() -> int:
    try:
        return int(getattr(_settings(), "SCHEMA_SHAPE_TTL_SECONDS", _DEFAULT_SHAPE_TTL))
    except Exception:  # pragma: no cover - defensive
        return _DEFAULT_SHAPE_TTL


def shape_state(
    db: Session,
    kb_id: str,
    *,
    force: bool = False,
    conn: Any = None,
) -> ShapeState:
    """Observe the KB's live shape, remembering it briefly.

    Fail-soft by design: an unreachable datasource, a missing driver or a dialect
    whose shape query is not implemented returns an EMPTY digest, which makes every
    caller fall back to the connection-identity key it used before this module
    existed. A probe failure must never make the schema look SMALLER than it is.

    ``conn`` lets a caller that already holds an open connector reuse it (the
    answering path does), so the probe does not cost a second connection.
    """
    if not bool(getattr(_settings(), "SCHEMA_SHAPE_ENABLED", True)):
        return ShapeState()

    ttl = _probe_ttl()
    fp = _fingerprint(db, kb_id)
    cache_key = (kb_id, fp)
    if ttl > 0 and not force:
        with _SHAPE_LOCK:
            hit = _SHAPE_CACHE.get(cache_key)
        if hit is not None:
            ts, digest, pairs = hit
            if time.monotonic() - ts <= ttl:
                return ShapeState(
                    digest=digest,
                    pairs=pairs,
                    changed=False,
                    from_cache=True,
                )

    pairs: tuple[tuple[str, str], ...] = ()
    error = ""
    try:
        if conn is not None:
            pairs = normalize_pairs(conn.shape())
        else:
            from app.models.knowledge_base import KnowledgeBase
            from app.services.db.connector_factory import get_connector

            kb = db.get(KnowledgeBase, kb_id) if db is not None else None
            if kb is None:
                return ShapeState(error="kb not found")
            with get_connector(kb) as fresh:
                pairs = normalize_pairs(fresh.shape())
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.debug("schema_shape: probe failed for KB %s: %s", kb_id, error)
        return ShapeState(error=error)

    digest = shape_digest(pairs)
    with _SHAPE_LOCK:
        previous = _LAST_DIGEST.get(kb_id, "")
        changed = bool(previous and digest and previous != digest)
        _LAST_DIGEST[kb_id] = digest
        if ttl > 0:
            _SHAPE_CACHE[cache_key] = (time.monotonic(), digest, pairs)
        elif ttl == 0:
            _SHAPE_CACHE.pop(cache_key, None)
    return ShapeState(
        digest=digest, pairs=pairs, changed=changed, previous=previous
    )


def live_shape_with(
    db: Session, kb_id: str, conn: Any, *, force: bool = False
) -> ShapeState:
    """Probe using an already-open connector (no second connection)."""
    return shape_state(db, kb_id, force=force, conn=conn)


# ── catalog baseline + drift ───────────────────────────────────────────────
def catalog_pairs(db: Session, kb_id: str) -> tuple[tuple[str, str], ...]:
    """The shape the CATALOG believes, from kb_table_meta + kb_column_meta.

    This is the drift baseline and it needs no new column and no migration: the
    catalog already records (table, column) for every indexed column.
    """
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

    try:
        rows = (
            db.query(KBTableMeta.table_name, KBColumnMeta.column_name)
            .join(KBColumnMeta, KBColumnMeta.table_meta_id == KBTableMeta.id)
            .filter(
                KBTableMeta.kb_id == kb_id,
                KBTableMeta.is_deleted.is_(False),
                KBColumnMeta.is_deleted.is_(False),
            )
            .all()
        )
    except Exception as e:  # pragma: no cover - catalog optional
        logger.debug("schema_shape: catalog read failed for KB %s: %s", kb_id, e)
        return ()
    return normalize_pairs(rows)


def catalog_digest(db: Session, kb_id: str) -> str:
    return shape_digest(catalog_pairs(db, kb_id))


def drift(
    db: Session,
    kb_id: str,
    live_pairs: Iterable[Sequence[str]] | None = None,
    *,
    cap: int = DEFAULT_DRIFT_CAP,
) -> dict[str, Any] | None:
    """Catalog-vs-live drift as a bounded dict, or None when in sync.

    Returns None when either side is unknown (an unindexed source has nothing to
    drift FROM, and an unreachable one has nothing to drift TO) — a missing
    measurement is reported as missing, never as agreement.
    """
    cat = catalog_pairs(db, kb_id)
    if not cat:
        return None
    if live_pairs is None:
        state = shape_state(db, kb_id)
        live_pairs = state.pairs
    if not live_pairs:
        return None
    report = diff_pairs(live_pairs, cat)
    if not report.drifted:
        return None
    return report.as_dict(cap=cap)
