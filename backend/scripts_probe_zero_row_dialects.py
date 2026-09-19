"""Does the zero-row coverage probe work on a non-MySQL dialect?

``nl_answer_service._probe_table_counts`` builds ``SELECT COUNT(*) AS cnt FROM
`table` `` — MySQL backtick quoting, hardcoded — with a 3 s budget. On any other
dialect the quoting is a syntax error, so the probe returns {} and the
zero-row/degenerate correction loses the evidence it exists to gather.

This calls the function itself against every database KB in the stack and
reports, per dialect, whether a count came back. Read-only.

    docker exec zhanlu-copy-backend sh -lc 'cd /app && python scripts_probe_zero_row_dialects.py > /tmp/zr.out 2>&1'
"""
from __future__ import annotations

import asyncio

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.nl_answer_service import _probe_table_counts


async def main() -> None:
    db = SessionLocal()
    kbs = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.source_kind == "database",
                KnowledgeBase.is_deleted == False)  # noqa: E712
        .all()
    )
    print(f"{'KB':<28}{'dialect':<12}{'table':<26}{'probe result'}")
    for kb in kbs:
        from app.services.db.connector_factory import get_connector
        try:
            with get_connector(kb) as conn:
                names = list(conn.list_tables())
        except Exception as e:
            print(f"{kb.name:<28}{kb.db_type:<12}{'-':<26}CONNECT FAIL {type(e).__name__}: {str(e)[:60]}")
            continue
        if not names:
            print(f"{kb.name:<28}{kb.db_type:<12}{'-':<26}NO TABLES")
            continue
        probe = await _probe_table_counts(db, kb.id, names[:3], timeout_s=8)
        shown = list(probe.items())[:3] or "(EMPTY — probe produced nothing)"
        print(f"{kb.name:<28}{kb.db_type:<12}{str(shown)[:70]}")


if __name__ == "__main__":
    asyncio.run(main())
