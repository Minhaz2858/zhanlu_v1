"""Golden eval harness — 10 hand-written Q→SQL cases scored through the REAL path.

These cases used to run against the legacy pipeline's own seams (its adapter, its
``DataSnapshotService.nl2sql`` call). They now run through ``nl2sql.ask()``, which
is a compatibility adapter over the canonical grounded path — so the score
measures the pipeline that answers users: source readiness, table linking, SQL
generation, the execution boundary, the grounded plan, the canonical result set.

The LLM is the only stubbed part (it needs a model); the source is a real SQLite
database, so the boundary introspects real tables and the connector really runs
the statements. A case that names a table or column the fixture does not have is
now REFUSED rather than counted as a pass — which is the point.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.agent_data_binding import AgentDataBinding  # noqa: F401
from app.models.data_snapshot import DataSnapshot  # noqa: F401
from app.models.knowledge_base import KnowledgeBase


def _load_cases():
    """Load golden cases from JSONL."""
    path = Path(__file__).parent / "golden" / "cases.jsonl"
    cases = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _build_golden_db(path: str) -> None:
    """Create a SQLite DB holding every table/column the golden cases name."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE customers (id INTEGER, name TEXT, region TEXT)")
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount REAL, order_date TEXT, product TEXT, total REAL)")
    con.execute("CREATE TABLE sales (id INTEGER, region TEXT, revenue REAL, order_date TEXT)")
    con.execute("INSERT INTO customers VALUES (1,'Alice','EU'),(2,'Bob','US'),(3,'Charlie','EU')")
    con.execute("INSERT INTO orders VALUES (1,1,100.0,'2026-07-01','Widget',100.0),(2,1,50.0,'2026-07-10','Gadget',50.0),(3,2,200.0,'2026-07-15','Widget',200.0)")
    con.execute("INSERT INTO sales VALUES (1,'EU',500.0,'2026-07-01'),(2,'US',300.0,'2026-07-05')")
    con.commit()
    con.close()


@pytest.fixture
def golden_env(monkeypatch):
    """A real source + the canonical path, with only the LLM call stubbed."""
    for flag, value in (
        ("KG_FRESHNESS_SHORTCIRCUIT_ENABLED", False),
        ("SCHEMA_LINKING_ENABLED", False),
        ("SEMANTIC_CATALOG_ENABLED", False),
        ("SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False),
        ("SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED", False),
        ("NL2SQL_SHADOW_MODE_ENABLED", False),
        ("NL2SQL_PLAN_MEMORY_ENABLED", False),
        ("NL2SQL_DB_ERROR_REPAIR_MAX", 0),
    ):
        if hasattr(settings, flag):
            monkeypatch.setattr(settings, flag, value)

    from app.services.db import query_router

    monkeypatch.setattr(
        query_router, "resolve_static_route", lambda *a, **k: None, raising=False
    )

    tmp = tempfile.NamedTemporaryFile(suffix=".golden.db", delete=False)
    tmp.close()
    _build_golden_db(tmp.name)

    apptmp = tempfile.NamedTemporaryFile(suffix=".appdb", delete=False)
    apptmp.close()
    eng = create_engine(f"sqlite:///{apptmp.name}")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(
        KnowledgeBase(
            id="kb-golden",
            name="golden source",
            type="database",
            source_kind="database",
            db_type="sqlite",
            file_url=tmp.name,
            database_name=tmp.name,
            catalog_status="ready",
            nl2sql_state="nl2sql_ready",
        )
    )
    db.commit()

    yield db, tmp.name

    db.close()
    eng.dispose()
    os.unlink(tmp.name)
    os.unlink(apptmp.name)


class TestGoldenEval:
    """Runs the 10-case golden eval suite through the canonical path."""

    def test_golden_eval_suite(self, golden_env, monkeypatch):
        from app.services.nl2sql import ask

        db, db_path = golden_env
        cases = _load_cases()
        assert len(cases) == 10, f"Expected 10 golden cases, got {len(cases)}"

        expected_map = {c["id"]: c["expected_sql"] for c in cases}
        asked: list[str] = []

        async def _text_to_sql(self, question, *args, **kwargs):
            asked.append(question)
            for case in cases:
                if case["question"] in question:
                    return expected_map[case["id"]]
            return "SELECT 1"

        monkeypatch.setattr(
            "app.services.db.nl_answer_service.NLAnswerService._text_to_sql",
            _text_to_sql,
        )

        results = []
        for case in cases:
            r = ask(case["question"], kb_id="kb-golden", db=db)
            results.append({
                "id": case["id"],
                "category": case["category"],
                "success": r.success,
                "error": r.error or r.disclosure,
                "error_kind": r.error_kind,
                "sql": r.sql,
                "rows": len(r.rows),
                "plan": bool(r.plan),
                "result_set": bool(r.result_set),
            })

        total = len(results)
        success = sum(1 for r in results if r["success"])
        accuracy = success / total if total > 0 else 0.0

        report = {
            "total_cases": total,
            "successful": success,
            "failed": total - success,
            "execution_accuracy": round(accuracy, 2),
            "failures": [
                {"id": r["id"], "category": r["category"],
                 "error": r["error"], "kind": r["error_kind"]}
                for r in results if not r["success"]
            ],
        }
        print(f"\nGolden Eval Report: {json.dumps(report, indent=2)}")

        # Every case must reach the boundary and run: the fixture holds every
        # table/column the cases name, so a refusal means the pipeline lost one.
        assert not report["failures"], report["failures"]
        assert success == total, f"Only {success}/{total} cases passed"
        # And every answer must carry the grounded contracts, not just rows.
        for r in results:
            assert r["plan"] and r["result_set"], f"case {r['id']} lost its contracts"
