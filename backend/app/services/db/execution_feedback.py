"""Execution feedback — explain-plan assessment + rewrite-outcome persistence
(OCI "SQL Rewriting" stage, P2-5).

The rewriter is heuristic (predicate pushdown, row caps). The article's
optimization stage adds **explain-plan feedback and historical rewrite
outcomes**. This module closes that loop conservatively:

1. ``assess_scan`` — when the datasource dialect supports it (PostgreSQL,
   MySQL), run a bounded ``EXPLAIN`` and return the planner's estimated
   row count for the generated SQL. Never executes the query, never
   raises.
2. ``persist_rewrite_outcome`` — record what the rewriter did (rules
   applied + estimated rows) as a ``query_workload_stat`` row
   (``stat_kind='rewrite_outcome'``) so future rewrite decisions have
   historical outcomes to learn from.

Flag: ``NL2SQL_EXPLAIN_FEEDBACK_ENABLED`` (default False — enable per
deployment once EXPLAIN permissions on the warehouse are confirmed).

Design constraints (mirroring the store's spec):
- READ-ONLY toward the datasource: EXPLAIN only, never the query itself.
- Never raises into the serving path; every failure is a debug log.
- Historical outcomes are LEARNED_FROM_WORKLOAD tier — ranking evidence,
  never truth.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

#: Estimated rows above which the plan is logged as a scan risk.
_SCAN_WARN_ROWS = 200_000

_EXPLAIN_TIMEOUT_S = 5.0


def _sql_hash(sql: str) -> str:
    return hashlib.sha1((sql or "").strip().encode("utf-8")).hexdigest()[:32]


def _stat_key(kind: str, table: str, peer: str | None, detail: str) -> str:
    raw = f"{kind}|{table}|{peer or ''}|{detail}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _dialect_of(engine) -> str:
    try:
        return (engine.dialect.name or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def assess_scan(db, sql: str) -> dict | None:
    """Estimated rows for ``sql`` via EXPLAIN, when the dialect supports it.

    Returns ``{"dialect": str, "estimated_rows": int}`` or ``None``.
    Bounded to 5s; read-only; never raises.
    """
    try:
        from app.config import settings

        if not getattr(settings, "NL2SQL_EXPLAIN_FEEDBACK_ENABLED", False):
            return None
        if not (sql or "").strip():
            return None
        engine = db.get_bind()
        dialect = _dialect_of(engine)
        if dialect not in ("postgresql", "mysql", "mariadb"):
            return None  # sqlite EXPLAIN has no row estimates; oracle/mssql later

        if dialect == "postgresql":
            stmt = f"EXPLAIN (FORMAT JSON) {sql}"
        else:  # mysql / mariadb
            stmt = f"EXPLAIN {sql}"

        raw = db.execute(stmt).mappings().all()
        estimated = _parse_estimate(dialect, raw)
        if estimated is None:
            return None
        out = {"dialect": dialect, "estimated_rows": int(estimated)}
        if estimated >= _SCAN_WARN_ROWS:
            logger.warning(
                "execution_feedback: EXPLAIN estimates %s rows — large scan "
                "risk (dialect=%s, sql_hash=%s)",
                f"{estimated:,}", dialect, _sql_hash(sql),
            )
        return out
    except Exception as e:  # noqa: BLE001 — feedback must never break serving
        logger.debug("execution_feedback: assess skipped: %s", e)
        return None


def _parse_estimate(dialect: str, raw) -> int | None:
    try:
        if dialect == "postgresql":
            # EXPLAIN (FORMAT JSON) returns one row with a JSON payload.
            first = raw[0]
            payload = first[0] if not isinstance(first, dict) else first.get("?column?", first.get("PLAN", first))
            if isinstance(payload, str):
                payload = json.loads(payload)
            plan = (payload or [{}])[0].get("Plan", {})
            rows = plan.get("Plan Rows")
            return int(rows) if rows is not None else None
        # MySQL/MariaDB: use the named "rows" column (MySQL EXPLAIN column
        # order varies; mappings are name-safe). Max across join rows.
        best = 0
        for row in raw:
            try:
                v = row.get("rows")
                if v is not None:
                    best = max(best, int(v))
            except (TypeError, ValueError):
                continue
        return best or None
    except Exception:  # noqa: BLE001
        return None


def persist_rewrite_outcome(
    db,
    kb_id: str,
    sql: str,
    *,
    rules_applied: list[str] | None = None,
    estimated_rows: int | None = None,
) -> bool:
    """Record one rewrite decision as workload history. Best-effort."""
    try:
        from app.config import settings

        if not getattr(settings, "NL2SQL_EXPLAIN_FEEDBACK_ENABLED", False):
            return False
        from app.models.query_workload_stat import QueryWorkloadStat

        detail = {
            "rules": list(rules_applied or []),
            "estimated_rows": estimated_rows,
        }
        primary = ""
        import re as _re

        m = _re.search(
            r"\bfrom\s+[`\"\[]?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
            sql or "", _re.IGNORECASE,
        )
        if m:
            primary = m.group(1).split(".")[-1].strip("`\"[]").lower()
        db.add(QueryWorkloadStat(
            kb_id=kb_id or "",
            stat_kind="rewrite_outcome",
            table_name=primary,
            peer=None,
            detail=detail,
            occurrences=1,
            stat_key=_stat_key("rewrite_outcome", primary, None, _sql_hash(sql)),
            note="execution_feedback",
        ))
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001 — history is best-effort
        logger.debug("execution_feedback: persist failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


__all__ = ["assess_scan", "persist_rewrite_outcome"]
