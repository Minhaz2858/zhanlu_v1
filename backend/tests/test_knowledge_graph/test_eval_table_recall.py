"""Eval — semantic catalog table recall + latency vs the raw describe_all baseline.

Run manually (needs warehouse access + a populated catalog):

    # Mode A — reuse the PRODUCTION catalog (fast, measures what's deployed):
    cd backend
    KG_EVAL_DATABASE_URL='postgresql+psycopg2://zhanlu:***@localhost:5400/zhanlu' \
    KG_EVAL_KB_ID=54119786-3718-40a5-b1a4-dc589944b2df \
    EDIA_MYSQL_URL='mysql+pymysql://root:***@10.10.10.49:3306/aipdp_data_warehouse_prod?charset=utf8mb4' \
    HF_HOME=$PWD/data/hf_cache HF_HUB_OFFLINE=1 \
    venv/bin/python -m pytest tests/test_knowledge_graph/test_eval_table_recall.py -v -s

    # Mode B — fresh index into a sqlite fixture (hermetic, slow: full re-index):
    EDIA_MYSQL_URL=... venv/bin/python -m pytest tests/test_knowledge_graph/test_eval_table_recall.py -v -s

Metrics: recall@5, recall@10, MRR for link_schema; per-question link_schema
latency; baseline describe_all latency; before/after delta vs the raw DDL dump.
"""

from __future__ import annotations

import os
import time
import uuid
from urllib.parse import unquote, urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.services.db.schema_service import SchemaService
from app.services.knowledge_graph.schema_linker import link_schema

from .eval_questions import EVAL_QUESTIONS

EDIA_MYSQL_URL = os.getenv("EDIA_MYSQL_URL", "").strip()
EVAL_DATABASE_URL = os.getenv("KG_EVAL_DATABASE_URL", "").strip()
EVAL_KB_ID = os.getenv("KG_EVAL_KB_ID", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not EDIA_MYSQL_URL,
        reason="EDIA_MYSQL_URL not set — needs warehouse + populated catalog",
    ),
    pytest.mark.skipif(
        bool(EVAL_DATABASE_URL) != bool(EVAL_KB_ID),
        reason="KG_EVAL_DATABASE_URL and KG_EVAL_KB_ID must be set together",
    ),
]


def _parse_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 3306,
        "database_name": (p.path or "/").lstrip("/"),
        "username": unquote(p.username or ""),
        "password": unquote(p.password or ""),
    }


@pytest.fixture
def db(tmp_path):
    """Session: production Postgres when KG_EVAL_DATABASE_URL is set, else sqlite."""
    if EVAL_DATABASE_URL:
        engine = create_engine(EVAL_DATABASE_URL)
    else:
        db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
        engine = create_engine(f"sqlite:///{db_file}")
        Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _recall_at(retrieved: list[str], expected: list[str], k: int) -> float:
    top = set(retrieved[:k])
    hits = sum(1 for t in expected if t in top)
    return hits / len(expected) if expected else 0.0


def _mrr(retrieved: list[str], expected: list[str]) -> float:
    for rank, table in enumerate(retrieved, start=1):
        if table in expected:
            return 1.0 / rank
    return 0.0


async def _get_or_build_kb(db) -> KnowledgeBase:
    """Mode A: fetch the existing production KB. Mode B: create + index a fresh one."""
    if EVAL_KB_ID:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == EVAL_KB_ID).first()
        assert kb is not None, f"KG_EVAL_KB_ID {EVAL_KB_ID} not found in target DB"
        assert kb.catalog_status == "ready", f"catalog_status={kb.catalog_status}"
        return kb

    from app.services.knowledge_graph.catalog_indexer import index_kb_catalog

    conn = _parse_url(EDIA_MYSQL_URL)
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        app_id="test-app",
        org_id="test-org",
        name="aipdp_data_warehouse_prod-eval",
        source_kind="db",
        db_type="mysql",
        host=conn["host"],
        port=conn["port"],
        database_name=conn["database_name"],
        username=conn["username"],
        password=conn["password"],
        catalog_status="pending",
    )
    db.add(kb)
    db.commit()
    await index_kb_catalog(kb, db)
    db.commit()
    db.refresh(kb)
    assert kb.catalog_status == "ready", f"indexing failed: {kb.catalog_status}"
    return kb


async def test_eval_table_recall(db):
    from app.config import settings

    kb = await _get_or_build_kb(db)

    old = settings.SCHEMA_LINKING_ENABLED
    settings.SCHEMA_LINKING_ENABLED = True
    try:
        # Baseline: raw DDL dump (the pre-catalog path) — timed.
        schema_svc = SchemaService(db)
        t0 = time.perf_counter()
        baseline = schema_svc.describe_all(kb.id, max_tables=50)
        baseline_ms = (time.perf_counter() - t0) * 1000
        baseline_names = {t["table"] for t in baseline.get("tables", [])}

        rows = []
        recall5, recall10, mrr_scores, latencies, catalog_better = [], [], [], [], 0
        for item in EVAL_QUESTIONS:
            question = item["question"]
            expected = item["expected_tables"]
            t0 = time.perf_counter()
            result = await link_schema(question, [kb.id], db, top_k=10)
            latency_ms = (time.perf_counter() - t0) * 1000
            latencies.append(latency_ms)
            retrieved = (
                [t["table_name"] for t in (result or {}).get("tables", [])]
                if result
                else []
            )
            r5 = _recall_at(retrieved, expected, 5)
            r10 = _recall_at(retrieved, expected, 10)
            m = _mrr(retrieved, expected)
            recall5.append(r5)
            recall10.append(r10)
            mrr_scores.append(m)

            # Before/after delta: does catalog surface more expected tables than raw DDL?
            baseline_hits = sum(1 for t in expected if t in baseline_names)
            catalog_hits = sum(1 for t in expected if t in retrieved)
            if catalog_hits > baseline_hits:
                catalog_better += 1

            rows.append(
                {
                    "question": question,
                    "expected": expected,
                    "retrieved": retrieved[:10],
                    "recall5": round(r5, 2),
                    "recall10": round(r10, 2),
                    "mrr": round(m, 2),
                    "ms": round(latency_ms),
                }
            )

        # ── print summary ──
        print("\n=== Semantic Catalog Eval (link_schema top-10) ===")
        print(f"{'question':<40} {'expected':<40} {'r@5':<5} {'r@10':<5} {'mrr':<5} {'ms':<6}")
        for r in rows:
            q = r["question"][:39]
            exp = ",".join(r["expected"])[:39]
            print(f"{q:<40} {exp:<40} {r['recall5']:<5} {r['recall10']:<5} {r['mrr']:<5} {r['ms']:<6}")

        n = len(rows)
        avg_r5 = sum(recall5) / n
        avg_r10 = sum(recall10) / n
        avg_mrr = sum(mrr_scores) / n
        pct_r10_ge_07 = sum(1 for x in recall10 if x >= 0.7) / n
        avg_lat = sum(latencies) / n
        p95_lat = sorted(latencies)[max(0, int(n * 0.95) - 1)]
        print(f"\navg recall@5 = {avg_r5:.2f}")
        print(f"avg recall@10 = {avg_r10:.2f}")
        print(f"avg MRR = {avg_mrr:.2f}")
        print(f"questions with recall@10 >= 0.7: {pct_r10_ge_07:.0%}")
        print(f"catalog beats raw DDL on {catalog_better}/{n} questions")
        print(f"\nlatency: link_schema avg {avg_lat:.0f}ms p95 {p95_lat:.0f}ms "
              f"vs describe_all(50) {baseline_ms:.0f}ms")

        # ── acceptance (Phase 1 gate) ──
        assert avg_r10 >= 0.9, f"avg recall@10 below gate: {avg_r10:.2f} < 0.90"
        assert pct_r10_ge_07 >= 0.8, (
            f"recall@10 >= 0.7 on only {pct_r10_ge_07:.0%} of questions"
        )
        assert avg_mrr >= 0.5, f"MRR too low: {avg_mrr:.2f}"
        assert catalog_better >= (2 * n) // 3, (
            f"catalog beats raw dump on only {catalog_better}/{n} questions"
        )
    finally:
        settings.SCHEMA_LINKING_ENABLED = old
