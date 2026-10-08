"""Workload Miner — offline enrichment of the NL2SQL query logs.

The OCI "behavioral level" of semantic enrichment: labeled/unlabeled query
logs carry signal — frequent joins, common filters, popular tables. This
module is the ONLY writer of ``query_workload_stats`` (the SemanticStore
facade stays read-only); it scans SUCCESSFUL logged executions, extracts
shape-only signals, and ACCUMULATES them per KB:

* ``table_pair``     — two tables co-occurring in one statement (a join
  candidate for linking and reachability evidence).
* ``table_filter``   — a recurring ``col = literal`` conjunct (the vocabulary
  and thresholds analysts actually filter on).
* ``popular_table``  — statement frequency (folded into SemanticStore's
  workload signal; popularity re-ranks, it never creates a candidate).

Rules: only successful rows are mined; reads/writes are strictly kb-scoped;
the scan is bounded; re-running accumulates (upsert on a stable stat_key),
never duplicates; anything shape-only — no dialect, no business vocabulary,
no table names in this file. The periodic runner follows the catalog
watchdog's asyncio pattern (no Celery in this platform).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

#: How many log rows one mining pass reads (bounded — never a full scan).
DEFAULT_SCAN_LIMIT = 2000
#: Longest literal kept in a filter detail.
_MAX_LITERAL_CHARS = 60
_LOOP_POLL_S = 1.0

# Same shape-only relation regex the SemanticStore workload scan uses:
# FROM/JOIN followed by an identifier. No dialect, no vocabulary.
_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+([`\"\[]?)([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
    re.IGNORECASE,
)
# A simple conjunct: alias.col = literal (string or number).
_FILTER_RE = re.compile(
    r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\s*=\s*('[^']*'|-?\d+(?:\.\d+)?)"
)
# Unqualified conjunct (no alias prefix) — attributable only when the
# statement names exactly ONE table, so the column's owner is unambiguous.
_FILTER_UNQUALIFIED_RE = re.compile(
    r"\b([A-Za-z_][\w$]*)\s*=\s*('[^']*'|-?\d+(?:\.\d+)?)"
)


def _stat_key(kind: str, table: str, peer: str | None, detail: str) -> str:
    raw = f"{kind}|{table}|{peer or ''}|{detail}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _table_names(sql: str) -> list[str]:
    seen: list[str] = []
    for _q, ident in _TABLE_REF_RE.findall(sql or ""):
        name = (ident or "").split(".")[-1].strip("`\"[]").lower()
        if name and name not in seen:
            seen.append(name)
    return seen


def _alias_map(sql: str) -> dict[str, str]:
    """alias → table for ``FROM/JOIN <table> <alias>`` in one statement."""
    out: dict[str, str] = {}
    for m in re.finditer(
        r"\b(?:from|join)\s+[`\"\[]?([A-Za-z_][\w$]*)[`\"\]]?"
        r"(?:\s+(?:AS\s+)?([A-Za-z_][\w$]*))?",
        sql or "",
        re.IGNORECASE,
    ):
        table = m.group(1).lower()
        alias = (m.group(2) or table).lower()
        if alias != "on" and alias != "where":
            out[alias] = table
    return out


def mine_query_workload(db: Session, *, scan_limit: int | None = None) -> dict:
    """One bounded mining pass over the successful query logs. Never raises."""
    empty = {"scanned": 0, "upserted": 0}
    if not getattr(settings, "NL2SQL_WORKLOAD_MINING_ENABLED", True):
        return empty
    if db is None:
        return empty
    try:
        from app.models.nl2sql_query_log import Nl2sqlQueryLog
        from app.models.query_workload_stat import QueryWorkloadStat

        limit = int(scan_limit or DEFAULT_SCAN_LIMIT)
        rows = (
            db.query(Nl2sqlQueryLog)
            .filter(
                Nl2sqlQueryLog.outcome == "success",
                Nl2sqlQueryLog.generated_sql.isnot(None),
            )
            .order_by(Nl2sqlQueryLog.created_date.desc())
            .limit(max(1, limit))
            .all()
        )
        # (kb, stat_key) → row accumulator
        acc: dict[tuple[str, str], dict] = {}

        def _add(kb: str, kind: str, table: str, peer: str | None, detail_json: dict | None, detail_str: str, seen_at) -> None:
            key = _stat_key(kind, table, peer, detail_str)
            slot = acc.get((kb, key))
            if slot is None:
                acc[(kb, key)] = {
                    "kb_id": kb,
                    "stat_kind": kind,
                    "table_name": table,
                    "peer": peer,
                    "detail": detail_json,
                    "detail_str": detail_str,
                    "occurrences": 1,
                    "last_seen_at": seen_at,
                }
            else:
                slot["occurrences"] += 1
                if seen_at and (slot["last_seen_at"] is None or seen_at > slot["last_seen_at"]):
                    slot["last_seen_at"] = seen_at

        scanned = 0
        for r in rows:
            sql = r.generated_sql or ""
            kb = r.datasource_id or ""
            if not sql.strip() or not kb:
                continue
            scanned += 1
            seen_at = getattr(r, "created_date", None) or datetime.utcnow()
            tables = _table_names(sql)
            if not tables:
                continue
            for t in tables:
                _add(kb, "popular_table", t, None, None, "", seen_at)
            # unordered pairs
            for i, a in enumerate(tables):
                for b in tables[i + 1 :]:
                    lo, hi = sorted((a, b))
                    _add(kb, "table_pair", lo, hi, None, "", seen_at)
            alias_map = _alias_map(sql)
            # On a single-table statement the SAME conjunct matches both the
            # qualified regex (t.status = 'open') and the unqualified one
            # (status = 'open'), producing an identical stat_key. Dedupe per
            # statement so one filter is counted once, not twice.
            seen_filters: set[tuple[str, str]] = set()
            for alias_m, col, literal in _FILTER_RE.findall(sql):
                table = alias_map.get(alias_m.lower())
                if not table or table not in tables:
                    continue
                lit = literal[:_MAX_LITERAL_CHARS]
                detail_str = f"{col} = {lit}"
                key = (table, detail_str)
                if key in seen_filters:
                    continue
                seen_filters.add(key)
                _add(kb, "table_filter", table, None, {"filter": detail_str}, detail_str, seen_at)
            if len(tables) == 1:
                # Unqualified conjuncts are unambiguous on a single-table statement.
                for col, literal in _FILTER_UNQUALIFIED_RE.findall(sql):
                    lit = literal[:_MAX_LITERAL_CHARS]
                    detail_str = f"{col} = {lit}"
                    key = (tables[0], detail_str)
                    if key in seen_filters:
                        continue
                    seen_filters.add(key)
                    _add(
                        kb, "table_filter", tables[0], None,
                        {"filter": detail_str}, detail_str, seen_at,
                    )

        upserted = 0
        for (kb, key), slot in acc.items():
            existing = (
                db.query(QueryWorkloadStat)
                .filter(
                    QueryWorkloadStat.kb_id == kb,
                    QueryWorkloadStat.stat_key == key,
                )
                .first()
            )
            if existing is None:
                db.add(
                    QueryWorkloadStat(
                        kb_id=kb,
                        stat_kind=slot["stat_kind"],
                        table_name=slot["table_name"],
                        peer=slot["peer"],
                        detail=slot["detail"],
                        occurrences=slot["occurrences"],
                        last_seen_at=slot["last_seen_at"],
                        stat_key=key,
                    )
                )
            else:
                existing.occurrences = (existing.occurrences or 0) + slot["occurrences"]
                if slot["last_seen_at"] and (
                    existing.last_seen_at is None or slot["last_seen_at"] > existing.last_seen_at
                ):
                    existing.last_seen_at = slot["last_seen_at"]
            upserted += 1
        db.commit()
        return {"scanned": scanned, "upserted": upserted}
    except Exception as e:
        logger.warning("workload_miner: pass failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return empty


async def run_workload_miner(
    stop_event: asyncio.Event,
    *,
    db=None,
    interval_s: int = 6 * 3600,
) -> None:
    """Periodic mining loop — the catalog-watchdog pattern, no Celery.

    Exits promptly when ``stop_event`` is already set (testability); each pass
    opens its own short session so a failure can never poison a long-lived one.
    """
    while not stop_event.is_set():
        try:
            if db is not None:
                mine_query_workload(db)
            else:
                from app.database import SessionLocal

                session = SessionLocal()
                try:
                    stats = mine_query_workload(session)
                    if stats.get("scanned"):
                        logger.info(
                            "workload_miner: mined %d statements → %d stats",
                            stats["scanned"], stats["upserted"],
                        )
                finally:
                    session.close()
        except Exception as e:  # a mining pass must never take the loop down
            logger.warning("workload_miner: loop pass failed: %s", e)
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=max(1, int(interval_s))
            )
        except asyncio.TimeoutError:
            continue
        break
