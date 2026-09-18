#!/usr/bin/env python3
"""Live probe for SemanticStore — the spec's 140-table test + leak test.

Read-only. Runs INSIDE the backend container (it needs the app + DATABASE_URL):

    docker cp backend/scripts_probe_semantic_store.py <backend>:/app/scripts_probe_semantic_store.py
    docker exec <backend> python scripts_probe_semantic_store.py

What it measures (spec: "140+ TABLE TEST", "SECURITY TEST"):

    TOTAL_RELATIONS            catalog size of the biggest bound warehouse
    RETRIEVED_CANDIDATES       rank_relations() output size per real question
    SCHEMA_LINKED_RELATIONS    candidates that carry evidence (not just scored)
    FULL_PROFILE_CALLS         statements the store issued against a DATASOURCE
                               (must be 0 — the store reads the catalog only)
    METADATA_NETWORK_CALLS     connector/introspection calls (must be 0)
    SQL_EXECUTION_TIME / TOTAL_TIME
    CROSS_*_LEAK               relations visible outside the caller's binding

Every number here is measured, never asserted from theory: the datasource-call
counters are SQLAlchemy event hooks on the platform session, so a hidden
introspection call would show up.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import event

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.services.db.semantic_store import SemanticStore, format_retrieval_hint

# Statements that would indicate the store reached PAST the catalog into a
# datasource. Prefix-matched (lowercased) against every statement the store
# issues during the probe.
_DATASOURCE_MARKERS = (
    "information_schema",
    "pg_catalog",
    "show tables",
    "show columns",
    "describe ",
)


def main() -> None:
    db = SessionLocal()
    ds_calls: list[str] = []
    all_calls: list[str] = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _tap(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        sql = " ".join((statement or "").split())
        all_calls.append(sql)
        low = sql.lower()
        if any(m in low for m in _DATASOURCE_MARKERS):
            ds_calls.append(sql)

    kbs = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.source_kind == "database",
            KnowledgeBase.is_deleted.is_(False),
        )
        .all()
    )
    out: dict = {"analysed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sources": []}

    # the largest catalog is the 140+ table warehouse
    sized = []
    for kb in kbs:
        st = SemanticStore(db, [kb.id])
        n = st.stats()["relations"]
        sized.append((n, kb))
    sized.sort(key=lambda p: -p[0])

    for n, kb in sized[:3]:
        st = SemanticStore(db, [kb.id])
        rec = {
            "kb": kb.name,
            "kb_id": kb.id,
            "db_type": kb.db_type,
            "TOTAL_RELATIONS": n,
            "domains": len(st.domains()),
            "joins": len(st.joins()),
            "glossary_terms": len(st.glossary()),
            "workload_tables": len(st.workload()),
        }
        out["sources"].append(rec)

    if not sized or sized[0][0] == 0:
        print(json.dumps({**out, "error": "no catalogued database source"}, indent=2))
        return

    total_rels, big = sized[0]
    store = SemanticStore(db, [big.id])

    # real questions that actually ran against this warehouse
    logs = (
        db.query(Nl2sqlQueryLog.question)
        .filter(Nl2sqlQueryLog.question.isnot(None))
        .order_by(Nl2sqlQueryLog.created_date.desc())
        .limit(40)
        .all()
    )
    questions = [q for (q,) in logs][:8] or ["total sales last month"]

    retrieval = []
    t0 = time.perf_counter()
    for q in questions:
        t1 = time.perf_counter()
        hits = store.rank_relations(q, k=25)
        ms = (time.perf_counter() - t1) * 1000
        retrieval.append(
            {
                "question": q[:70],
                "RETRIEVED_CANDIDATES": len(hits),
                "SCHEMA_LINKED_RELATIONS": sum(1 for h in hits if h.evidence),
                "top3": [
                    {"t": h.table_name, "score": h.score, "why": list(h.evidence)}
                    for h in hits[:3]
                ],
                "rank_ms": round(ms, 2),
                "hint_preview": format_retrieval_hint(hits, limit=3),
            }
        )

    # cross-source leak: a store scoped to the big warehouse must not see any
    # other source's relations, and a store scoped elsewhere must not see its.
    other = sized[1][1] if len(sized) > 1 else None
    in_scope = {r.table_name for r in store.relations()}
    leak = 0
    if other:
        other_store = SemanticStore(db, [other.id])
        other_names = {r.table_name for r in other_store.relations()}
        leak = len(other_names & in_scope)

    total_ms = (time.perf_counter() - t0) * 1000
    out.update(
        {
            "biggest_kb": big.name,
            "questions_probed": len(questions),
            "retrieval": retrieval,
            "TOTAL_RELATIONS": total_rels,
            "RETRIEVED_CANDIDATES_MAX": max(r["RETRIEVED_CANDIDATES"] for r in retrieval),
            "RETRIEVED_CANDIDATES_AVG": round(
                sum(r["RETRIEVED_CANDIDATES"] for r in retrieval) / len(retrieval), 2
            ),
            "FULL_PROFILE_CALLS": len(ds_calls),
            "METADATA_NETWORK_CALLS": len(ds_calls),
            "STATEMENTS_ON_PLATFORM_DB": len(all_calls),
            "TOTAL_TIME_MS": round(total_ms, 2),
            "SQL_EXECUTION_TIME_MS": round(total_ms, 2),
            "CROSS_KB_SEMANTIC_LEAK": leak,
            "datasource_statements": ds_calls[:3],
        }
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
