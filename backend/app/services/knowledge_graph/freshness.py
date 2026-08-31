"""Generic table-freshness detection for NL2SQL schema selection.

Computes the newest date value per table from the LIVE database (not the
catalog snapshot, which can lag behind reality). The schema linker then
flags tables whose data stopped arriving (``STALE``) or that have never
had rows (``EMPTY``), so the LLM stops picking dead tables for questions
about recent periods.

Database-agnostic: works for any KnowledgeBase the platform can connect
to (MySQL, PostgreSQL, ...). No hardcoded table names — the date column
is detected from the catalog's column metadata, and MAX() is executed
through the same generic connector the query tools use.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# A table whose newest row is older than this is flagged STALE in prompts.
STALE_THRESHOLD_DAYS = int(os.environ.get("SCHEMA_FRESHNESS_STALE_DAYS", "60"))
# MAX(date) results are cached this long (seconds) to amortize WAN round-trips.
_CACHE_TTL_S = int(os.environ.get("SCHEMA_FRESHNESS_CACHE_TTL_S", "600"))
# Cap tables probed per call — the schema linker only ever enriches top-K
# candidates, so this is a safety net, not the normal path.
_MAX_TABLES_PER_CALL = 16
# Fail-soft per-query timeout.
_QUERY_TIMEOUT_S = 8

_DATE_TYPE_HINTS = ("date", "time", "timestamp", "datetime", "year")
_DATE_NAME_HINTS = ("date", "time", "day", "dt", "month", "period")

_lock = threading.Lock()
_cache: dict[str, dict] = {}  # key -> {"max_date": iso|None, "at": epoch}


def _cache_get(key: str):
    with _lock:
        hit = _cache.get(key)
        if not hit:
            return None
        if time.time() - hit["at"] > _CACHE_TTL_S:
            _cache.pop(key, None)
            return None
        return hit["max_date"]


def _cache_put(key: str, value) -> None:
    with _lock:
        _cache[key] = {"max_date": value, "at": time.time()}


def date_columns(columns: list[dict]) -> list[str]:
    """Detect likely date/datetime columns from catalog column metadata."""
    out: list[str] = []
    for c in columns or []:
        name = str(c.get("name") or "").lstrip("\ufeff").strip()
        if not name:
            continue
        dtype = (c.get("data_type") or "").lower()
        lname = name.lower()
        if any(h in dtype for h in _DATE_TYPE_HINTS):
            out.append(name)
        elif any(h in lname for h in _DATE_NAME_HINTS):
            out.append(name)
    # De-duplicate, keep order
    seen: set[str] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


_IDENT_RE = re.compile(r"^[\w\u4e00-\u9fff$.]+$")


def _quote(name: str, db_type: Optional[str]) -> Optional[str]:
    """Quote an identifier for the KB's dialect; None if unsafe."""
    name = str(name).lstrip("\ufeff").strip()
    if not _IDENT_RE.match(name):
        return None
    if (db_type or "").startswith("postgres"):
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def table_max_date(db, kb_id: str, table_name: str, col_name: str) -> Optional[str]:
    """Live ``SELECT MAX(col) FROM table`` with TTL cache; None on any failure."""
    key = f"{kb_id}|{table_name}|{col_name}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from app.models.knowledge_base import KnowledgeBase
        from app.services.db.connector_factory import get_connector

        kb = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if kb is None or (kb.source_kind or "").lower() != "database":
            return None
        tq = _quote(table_name, kb.db_type)
        cq = _quote(col_name, kb.db_type)
        if not tq or not cq:
            return None
        with get_connector(kb) as conn:
            rows = conn.execute(
                f"SELECT MAX({cq}) AS m FROM {tq}",
                max_rows=1,
                timeout_s=_QUERY_TIMEOUT_S,
            )
        raw = rows[0].get("m") if rows else None
        value: Optional[str] = None
        if raw is not None:
            if isinstance(raw, (datetime, date)):
                value = raw.isoformat()[:10]
            else:
                value = str(raw)[:10]
        _cache_put(key, value)
        return value
    except Exception as exc:  # noqa: BLE001 — freshness is best-effort
        logger.debug("freshness: MAX(%s.%s) failed: %s", table_name, col_name, exc)
        return None


def annotate_tables(db, kb_id: str, tables: list[dict]) -> list[dict]:
    """Mutate each table dict with ``last_data_date`` / ``stale_days``.

    Only tables with a detectable date column are probed; everything else
    is left untouched. Best-effort: any failure simply leaves the table
    unannotated (the LLM still sees the plain schema).
    """
    today = date.today()
    for t in tables[:_MAX_TABLES_PER_CALL]:
        dcols = date_columns(t.get("columns") or [])
        if not dcols:
            continue
        md = table_max_date(db, kb_id, t.get("table_name", ""), dcols[0])
        if md is None:
            continue
        t["last_data_date"] = md
        try:
            t["stale_days"] = (today - date.fromisoformat(md)).days
        except ValueError:
            t["stale_days"] = None
    return tables


def stale_flag(table: dict) -> str:
    """Short prompt marker for a table dict; '' when fresh/unknown."""
    md = table.get("last_data_date")
    if not md:
        return ""
    days = table.get("stale_days")
    if days is None:
        return f" (newest data {md})"
    if days > STALE_THRESHOLD_DAYS:
        return f" ⚠️STALE: newest data {md} ({days} days ago)"
    return f" (newest data {md})"


def annotate_tables_parallel(
    db, kb_id: str, tables: list[dict], max_workers: int = 8
) -> list[dict]:
    """Like ``annotate_tables`` but probes MAX(date) concurrently.

    Used where the candidate list can be large (e.g. describe_all) so the
    WAN round-trips overlap instead of serializing. Same TTL cache and
    fail-soft semantics.
    """
    import concurrent.futures as _futures

    probed: list[tuple[dict, str]] = []
    for t in tables[:_MAX_TABLES_PER_CALL]:
        dcols = date_columns(t.get("columns") or [])
        if dcols:
            probed.append((t, dcols[0]))

    def _probe(item) -> tuple[dict, Optional[str]]:
        t, col = item
        return t, table_max_date(db, kb_id, t.get("table_name", ""), col)

    if probed:
        today = date.today()
        with _futures.ThreadPoolExecutor(
            max_workers=min(max_workers, max(1, len(probed)))
        ) as ex:
            for t, md in ex.map(_probe, probed):
                if md is None:
                    continue
                t["last_data_date"] = md
                try:
                    t["stale_days"] = (today - date.fromisoformat(md)).days
                except ValueError:
                    t["stale_days"] = None
    return tables
