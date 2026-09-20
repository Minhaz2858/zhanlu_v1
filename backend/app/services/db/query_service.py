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
        #: optional enforcement CONTEXT — the resolved policy and the grounded
        #: plan are what the gate validates against. Neither is a switch: the gate
        #: runs with or without them (see db/execution_gate.py).
        self.policy = policy
        self.plan = plan
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

        # ── the execution boundary validates, unconditionally ──────────────
        # Every caller funnels through execute(): the sealed NL path, the agent's
        # execute_query tool, dashboards, report recipes, forecasting probes. The
        # checks live HERE rather than in whichever caller remembered to ask, so a
        # model-authored table or column is refused before the driver sees it.
        from app.services.db import execution_gate

        self.last_validation = execution_gate.enforce(
            self._db, kb_id, sql, policy=self.policy, plan=self.plan
        )

        t0 = time.time()
        with get_connector(kb) as conn:
            # +1 to PROVE truncation (see docstring).
            fetched = conn.execute(sql, max_rows=row_cap + 1, timeout_s=timeout_cap)
            # The connectors differ in what they hand the driver — MySQL prepends
            # the MAX_EXECUTION_TIME hint, Oracle wraps with ROWNUM, Postgres and
            # the rest execute the authored text verbatim — so the executable
            # statement is read back from the connector rather than assumed.
            executed_sql = str(getattr(conn, "last_executed_sql", "") or sql)
        elapsed_ms = int((time.time() - t0) * 1000)
        validation = dict(self.last_validation or {})
        if validation:
            # Provenance: the authored statement was validated above; the statement
            # the driver actually received is read back from the connector.
            validation["executed_sql"] = executed_sql

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
