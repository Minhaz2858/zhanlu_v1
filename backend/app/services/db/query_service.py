"""QueryService — wraps a connector for safe SQL execution.

The connector caps (`MAX_QUERY_ROWS_CAP` / `MAX_QUERY_TIMEOUT_S`) and the
read-only session settings live here and in the connectors, so nothing above
this layer can widen them: callers pass caps, this clamps them.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase
from app.services.db.connector_factory import get_connector

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 1000
DEFAULT_TIMEOUT_S = 10


class QueryService:
    """Run a SQL statement against a KnowledgeBase and return rows."""

    def __init__(self, db: Session, *, policy: Any = None, plan: Any = None):
        self._db = db
        #: optional enforcement CONTEXT — the resolved policy, the grounded plan
        #: and the row filters are what the gate validates against. None of them
        #: is a switch: the gate runs with or without them (see db/execution_gate.py).
        self.policy = policy
        self.plan = plan
        #: 2B-3 scope context ({"classification_facts", "question_scope"}) the
        #: gate validates table scope against. None/empty == flag off or the
        #: loader degraded — the check records not-applicable, never refuses.
        self.scope_context: dict | None = None
        #: row filters (dicts) the statement must carry for the query to be a
        #: filtered query rather than an unfiltered one. Empty == none configured.
        self.row_filters: list[Any] = []
        #: the payload of the validation this execution passed, for provenance
        self.last_validation: dict | None = None

    def _load_kb(self, kb_id: str) -> KnowledgeBase:
        kb = self._db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,
        ).first()
        if not kb:
            raise ValueError(f"KnowledgeBase not found: {kb_id}")
        if (kb.source_kind or "").lower() != "database":
            raise ValueError(
                f"KnowledgeBase {kb_id!r} is not a database source "
                f"(source_kind={kb.source_kind!r})"
            )
        return kb

    def execute(
        self,
        kb_id: str,
        sql: str,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        params: dict | None = None,
    ) -> dict:
        """Run `sql` against the KB and return rows + metadata.

        Two things this deliberately does NOT trust:

        * the caller's caps — `max_rows`/`timeout_s` arrive from model-authored
          tool args, so they are clamped here (``MAX_QUERY_ROWS_CAP`` /
          ``MAX_QUERY_TIMEOUT_S``). Callers below this line cannot raise them.
        * the row count as a truncation proof — one extra row is fetched and
          discarded, because ``len(rows) == cap`` cannot distinguish "the data
          ends here" from "the cap bit". That false positive used to tell the
          agent an exact-cap result was truncated.

        Returns:
            {
                "source": {"id", "name", "db_type"},
                "sql": str,
                "executed_sql": str,   # the statement the driver received
                "rows": [...],
                "row_count": int,        # rows returned (== len(rows))
                "rows_returned": int,    # explicit alias for the above
                "row_cap": int,          # the cap actually applied
                "truncated": bool,       # proven: more rows existed
                "elapsed_ms": int,
            }
        """
        import time
        if not sql or not sql.strip():
            raise ValueError("Empty SQL")
        # Strip trailing semicolons (some drivers reject multi-statements)
        sql = sql.strip().rstrip(";").strip()

        try:
            row_cap = max(1, min(int(max_rows), int(settings.MAX_QUERY_ROWS_CAP)))
        except Exception:
            row_cap = DEFAULT_MAX_ROWS
        try:
            timeout_cap = max(1, min(int(timeout_s), int(settings.MAX_QUERY_TIMEOUT_S)))
        except Exception:
            timeout_cap = DEFAULT_TIMEOUT_S

        kb = self._load_kb(kb_id)

        # ── Timeout policy (2026-10-10): QUERY_TIMEOUT_DISABLED removes the
        # per-statement timeout entirely — connectors treat timeout_s <= 0 as
        # "run unbounded" (no MAX_EXECUTION_TIME hint, no SET
        # statement_timeout, no SQLite progress handler, no MSSQL
        # LOCK_TIMEOUT). For very large warehouses where 30s kills
        # legitimate aggregations.
        from app.config import settings as _settings

        if getattr(_settings, "QUERY_TIMEOUT_DISABLED", False):
            timeout_cap = 0
        else:
            try:
                timeout_cap = max(1, min(int(timeout_s), int(settings.MAX_QUERY_TIMEOUT_S)))
            except Exception:
                timeout_cap = DEFAULT_TIMEOUT_S

        # ── the execution boundary validates, unconditionally ──────────────
        # Every caller funnels through execute(): the sealed NL path, the agent's
        # execute_query tool, dashboards, report recipes, forecasting probes. The
        # checks live HERE rather than in whichever caller remembered to ask, so a
        # model-authored table or column is refused before the driver sees it.
        from app.services.db import execution_gate

        self.last_validation = execution_gate.enforce(
            self._db,
            kb_id,
            sql,
            policy=self.policy,
            plan=self.plan,
            # Empty means "this caller configured none" — the gate then falls
            # back to its own configured-filter lookup rather than skipping the
            # check. ``None`` (not ``[]``) is what keeps that discovery alive.
            row_filters=(list(self.row_filters) if self.row_filters else None),
            scope_context=self.scope_context,
        )

        t0 = time.time()
        collation_healed = False
        syntax_healed = False
        with get_connector(kb) as conn:
            # +1 to PROVE truncation (see docstring).
            try:
                fetched = conn.execute(
                    sql, params=params, max_rows=row_cap + 1, timeout_s=timeout_cap
                )
                executed_sql = str(getattr(conn, "last_executed_sql", "") or sql)
            except Exception as exc:
                # ── Self-healing stage (one attempt per error class) ──
                # 1. Collation heal (2026-10-10): MySQL ER 1267 — rewrite
                #    cross-table join predicates with CONVERT(col USING
                #    <charset>) and retry ONCE.
                # 2. Syntax heal (2026-10-19): MySQL ER 1064 caused by a
                #    reserved-word alias (measured live: ``AS lines``) —
                #    quote the aliases the ENGINE blamed (near-text) plus
                #    every other reserved-word alias, retry ONCE.
                # Anything else re-raises.
                from app.config import settings as _hs
                from app.services.db import collation_heal as _ch
                from app.services.db import syntax_heal as _sh

                healed = None
                healed_kind = None
                if (
                    getattr(_hs, "COLLATION_HEAL_ENABLED", True)
                    and _ch.is_collation_error(exc)
                ):
                    healed = _ch.heal_join_collations(
                        sql, (kb.db_type or "mysql").lower(),
                        charset=getattr(_hs, "COLLATION_TARGET_CHARSET", "utf8mb4"),
                    )
                    healed_kind = "collation"
                if healed is None and getattr(
                    _hs, "SQL_SYNTAX_HEAL_ENABLED", True
                ):
                    healed = _sh.heal_syntax(
                        sql, exc, (kb.db_type or "").lower()
                    )
                    healed_kind = "syntax"
                if not healed or healed == sql:
                    raise
                logger.warning(
                    "query_service: %s self-heal on KB %s (%s) — retrying "
                    "with healed statement",
                    healed_kind, kb_id, str(exc)[:160],
                )
                fetched = conn.execute(
                    healed, params=params, max_rows=row_cap + 1, timeout_s=timeout_cap
                )
                executed_sql = str(getattr(conn, "last_executed_sql", "") or healed)
                collation_healed = healed_kind == "collation"
                syntax_healed = healed_kind == "syntax"
            # The connectors differ in what they hand the driver — MySQL prepends
            # the MAX_EXECUTION_TIME hint, Oracle wraps with ROWNUM, Postgres and
            # the rest execute the authored text verbatim — so the executable
            # statement is read back from the connector rather than assumed.
        elapsed_ms = int((time.time() - t0) * 1000)
        validation = dict(self.last_validation or {})
        if collation_healed:
            validation["collation_healed"] = True
        if syntax_healed:
            validation["syntax_healed"] = True
        if validation:
            # Provenance: the authored statement was validated above; the statement
            # the driver actually received is read back from the connector.
            validation["executed_sql"] = executed_sql

        # ── OCI optimization stage: execution feedback (2026-10-11 wiring) ──
        # The module existed but had NO caller. Two bounded, read-only
        # feedback writes after every successful execution (both flag-gated,
        # both fail-soft): a plan-row estimate attached to the result, and a
        # rewrite-outcome history row so future rewrite decisions can learn.
        try:
            from app.services.db import execution_feedback as _ef

            _assessment = _ef.assess_scan(
                getattr(conn, "_engine", None), executed_sql
            )
            if _assessment:
                validation["plan_estimate"] = _assessment
            _ef.persist_rewrite_outcome(
                kb_id,
                executed_sql,
                estimated_rows=(_assessment or {}).get("estimated_rows"),
                elapsed_ms=elapsed_ms,
                row_count=min(len(fetched), row_cap),
            )
        except Exception:  # noqa: BLE001 — feedback never breaks serving
            pass

        truncated = len(fetched) > row_cap
        rows = fetched[:row_cap] if truncated else fetched
        return {
            "source": {
                "id": kb.id,
                "name": kb.name,
                "db_type": kb.db_type,
            },
            "sql": sql,
            #: what the database actually ran, when a dialect rewrite applies
            "executed_sql": executed_sql,
            "rows": rows,
            "row_count": len(rows),
            "rows_returned": len(rows),
            "row_cap": row_cap,
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
            #: what was verified before the statement was sent, and by which checks
            "validation": validation,
        }
