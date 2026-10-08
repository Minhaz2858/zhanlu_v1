"""Workload Mining — offline enrichment of the query logs (OCI behavioral level).

Pins: only successful executions are mined; everything is kb-scoped; table
pairs are normalised (unordered); re-running ACCUMULATES occurrences instead
of duplicating rows; the scan is bounded; the flag gates the whole pass; and
the SemanticStore folds mined popularity into its workload signals without
letting popularity create candidates (that rule lives in rank_relations).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.base import TimestampedBase
from app.models.knowledge_catalog import KBTableMeta
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.query_workload_stat import QueryWorkloadStat
from app.services.knowledge_graph.workload_miner import (
    mine_query_workload,
    run_workload_miner,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _log(session, sql, *, kb="kb1", outcome="success"):
    session.add(
        Nl2sqlQueryLog(
            datasource_id=kb, question="q", generated_sql=sql, outcome=outcome
        )
    )
    session.commit()


def _stats(session, kb="kb1"):
    return session.query(QueryWorkloadStat).filter(QueryWorkloadStat.kb_id == kb).all()


# ── mining ────────────────────────────────────────────────────────────────


def test_table_pairs_mined_from_join_statements(db):
    join_sql = "SELECT a.id FROM orders a INNER JOIN customers b ON b.id = a.customer_id"
    _log(db, join_sql)
    _log(db, join_sql.replace("a.id", "a.name"))
    result = mine_query_workload(db)
    assert result["scanned"] == 2
    pairs = [s for s in _stats(db) if s.stat_kind == "table_pair"]
    assert len(pairs) == 1, "the same pair accumulates into ONE row"
    assert pairs[0].occurrences == 2
    assert {pairs[0].table_name, pairs[0].peer} == {"orders", "customers"}


def test_filters_mined_from_where_literals(db):
    _log(db, "SELECT id FROM orders WHERE status = 'open' AND region = 'EU'")
    mine_query_workload(db)
    filters = [s for s in _stats(db) if s.stat_kind == "table_filter"]
    detail_texts = [(s.detail or {}).get("filter", "") for s in filters]
    assert any("status = 'open'" in t for t in detail_texts)
    assert any("region = 'EU'" in t for t in detail_texts)


def test_failed_rows_are_skipped(db):
    _log(db, "SELECT a FROM x JOIN y ON y.id = a", outcome="error")
    mine_query_workload(db)
    assert _stats(db) == []


def test_strictly_kb_scoped(db):
    _log(db, "SELECT a FROM x JOIN y ON y.id = a", kb="kb2")
    mine_query_workload(db)
    assert _stats(db, kb="kb1") == []
    assert _stats(db, kb="kb2"), "the owning kb keeps its own mined stats"


def test_idempotent_rerun_accumulates_not_duplicates(db):
    _log(db, "SELECT a FROM x JOIN y ON y.id = a WHERE status = 'open'")
    mine_query_workload(db)
    first = {s.stat_key: s.occurrences for s in _stats(db)}
    mine_query_workload(db)
    rows = _stats(db)
    second = {s.stat_key: s.occurrences for s in rows}
    assert len(second) == len(first), "no duplicate rows on re-run"
    assert all(second[k] > first[k] for k in first)


def test_bounded_scan(db):
    for _ in range(5):
        _log(db, "SELECT a FROM x JOIN y ON y.id = a")
    result = mine_query_workload(db, scan_limit=2)
    assert result["scanned"] == 2


def test_flag_gates_the_pass(monkeypatch, db):
    _log(db, "SELECT a FROM x JOIN y ON y.id = a")
    monkeypatch.setattr(settings, "NL2SQL_WORKLOAD_MINING_ENABLED", False)
    assert mine_query_workload(db)["scanned"] == 0
    assert _stats(db) == []


def test_never_raises_on_garbage_sql(db):
    _log(db, "not even sql !!! (((")
    mine_query_workload(db)  # must not raise; may produce zero stats


# ── the background loop ───────────────────────────────────────────────────


def test_runner_loop_exits_when_stop_already_set(db):
    import asyncio

    async def _go():
        stop = asyncio.Event()
        stop.set()
        await run_workload_miner(stop, db=db, interval_s=1)

    asyncio.run(_go())  # returns instead of hanging


# ── semantic store consumption ────────────────────────────────────────────


def test_semantic_store_folds_mined_popularity(db):
    db.add(
        KBTableMeta(
            id="tm1", kb_id="kb1", schema_name="", table_name="orders",
            table_type="TABLE",
        )
    )
    db.commit()
    _log(db, "SELECT id FROM orders WHERE status = 'open'")
    _log(db, "SELECT id FROM orders WHERE status = 'open'")
    mine_query_workload(db)

    from app.services.db.semantic_store import SemanticStore

    store = SemanticStore(db, ["kb1"])
    wl = store.workload()
    assert "orders" in wl
    assert wl["orders"].uses >= 2
    assert wl["orders"].successes >= 2
