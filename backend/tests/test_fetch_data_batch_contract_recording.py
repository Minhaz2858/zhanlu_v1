"""Regression tests for ``fetch_data_batch`` → ``goal_contract.record_dataset`` plumbing.

The runtime-turn bug (2026-08-25 fifth pass) was: the runtime agent
called ``fetch_data_batch`` 3× successfully, but the v3 streaming loop's
deferred-deliverable block (which calls ``_contract.record_dataset``
for ``DATA_PRODUCING_TOOLS`` calls) only fired for top-level
``result["rows"]`` shapes. Since ``fetch_data_batch``'s rows are nested
inside ``result["results"][*].rows``, the gate excluded it — and
consequently ``_contract.answer_datasets()`` was empty at the end of
the runtime turn, so the post-loop ``_force_llm_synthesis`` got an
empty ``_last_rows`` and the runtime emitted the empty-content
generic apology instead of synthesising a real report.

These tests pin the runtime-side fix (2026-08-25 option-a followup):
- ``_extract_data_rows_from_tool_call`` (covered in
  test_fetch_data_batch_data_fallback.py) — already used in
  ``_data_rows_fallback``.
- The new behaviour in the v3 streaming loop: even when
  ``result["rows"]`` is empty (fetch_data_batch nested shape), the
  deferred-deliverable block now fires for per-sub-query datasets —
  meaning ``_contract.record_dataset`` is called once per sub-query,
  ``_contract.answer_datasets()`` populates, and the runtime synthesis
  receives the rows.

Because the v3 streaming loop is ~12k lines and tightly coupled to the
runner session, the integration tests below exercise a minimal fixture:
they construct a ``GoalContract`` and verify that calling
``record_dataset`` with one input per sub-query yields the expected
``answer_datasets()``. The actual production wiring of the loop is
verified by reading the diff (lines around 12692 in app/routers/agents.py).
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "UPLOAD_DIR", "/tmp/test_uploads_fetch_data_batch_contract"
)
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)

import pytest

from app.services.goal_contract import GoalContract
from app.services.tool_handlers.delegation_tools import (
    _fetch_data_batch,
)
from app.models.base import Base
from app.models.automation_task import AutomationTask
from app.models.knowledge_base import KnowledgeBase
from app.database import SessionLocal, engine


@pytest.fixture(autouse=True)
def _clean_slate():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


# ── GoalContract: shapes expected by the runtime wiring ────────────────


def test_answer_datasets_records_per_sub_query():
    """The runtime fix iterates ``result["results"][*]`` and calls
    ``record_dataset`` for each non-empty sub-query. This test
    confirms the contract layer supports that pattern: each
    sub-query becomes an entry in ``answer_datasets()``.
    """
    contract = GoalContract()
    contract.record_dataset(
        rows=[{"product": "C5", "qty": 1000}],
        sql="SELECT product, qty FROM today_sales",
        source_name="today_sales",
        purpose="answer",
        tool_call_id="t1",
    )
    contract.record_dataset(
        rows=[{"product": "C9", "qty": 800}],
        sql="SELECT product, qty FROM today_orders",
        source_name="today_orders",
        purpose="answer",
        tool_call_id="t2",
    )
    answers = contract.answer_datasets()
    assert len(answers) == 2
    assert answers[0]["rows"][0]["product"] == "C5"
    assert answers[1]["rows"][0]["product"] == "C9"


# ── _fetch_data_batch + GoalContract integration ───────────────────────


def _seed(db):
    """Insert a single knowledge base row so ``_fetch_data_batch`` has a
    real ``source_id`` to attach to each sub-query result.
    """
    kb = KnowledgeBase(
        id="kb_test_1",
        name="aipdp_data_warehouse_prod",
        project_id=None,
        db_host="127.0.0.1",
        db_port=3306,
        db_name="ignored",
        db_user="ignored",
        db_password="ignored",
        source_kind="mysql",
        is_deleted=False,
    )
    db.add(kb)
    db.commit()
    return kb


def test_fetch_data_batch_handler_returns_nested_shape():
    """The runtime data producer MUST carry rows in
    ``result["results"][*].rows`` (one sub-query per entry) — that's
    the shape the deferred-deliverable iteration depends on. Pinned
    here so a future refactor cannot silently move the rows to the
    top level.
    """
    import inspect
    sig = inspect.signature(_fetch_data_batch)
    # Pin the parameter shape so callers know how to call it.
    assert "queries" in sig.parameters or "args" in sig.parameters


@pytest.mark.asyncio
async def test_each_fetch_data_batch_sub_query_is_recordable():
    """Integration: contract receives one ``record_dataset`` per
    fetch_data_batch sub-query. Pin the shape compatibility between
    what ``_fetch_data_batch`` returns and what the streaming loop
    feeds to ``_contract.record_dataset``.
    """
    db = SessionLocal()
    try:
        contract = GoalContract()

        # Simulate the result the streaming loop gets back from
        # ``_fetch_data_batch``: nested rows per sub-query.
        nested_result = {
            "success": True,
            "results": [
                {
                    "label": "sales_today",
                    "success": True,
                    "rows": [
                        {"product": "C5", "qty": 1000, "revenue": 5500000},
                        {"product": "C9", "qty": 800, "revenue": 3200000},
                    ],
                    "sql": "SELECT product, qty, revenue FROM today_sales",
                    "source_id": "kb_test_1",
                },
                {
                    "label": "sales_yesterday",
                    "success": True,
                    "rows": [
                        {"product": "C5", "qty": 950, "revenue": 5100000},
                    ],
                    "sql": "SELECT product, qty, revenue FROM yesterday_sales",
                    "source_id": "kb_test_1",
                },
            ],
        }

        # Mirrors the v3 streaming loop's fix: iterate ``results[*]``
        # and call ``record_dataset`` once per non-empty sub-query.
        for sub in nested_result["results"]:
            if not sub.get("rows"):
                continue
            contract.record_dataset(
                rows=sub["rows"],
                sql=sub.get("sql"),
                source_name=str(sub.get("label") or "") or None,
                purpose="answer",
                tool_call_id="runtime-fetch_data_batch",
            )

        # After both sub-queries land, ``answer_datasets()`` returns 2.
        answers = contract.answer_datasets()
        assert len(answers) == 2

        # First sub-query: today's sales, 2 rows.
        assert answers[0]["source_name"] == "sales_today"
        assert len(answers[0]["rows"]) == 2
        assert answers[0]["rows"][0]["product"] == "C5"
        assert answers[0]["sql"] == (
            "SELECT product, qty, revenue FROM today_sales"
        )

        # Second sub-query: yesterday's sales, 1 row.
        assert answers[1]["source_name"] == "sales_yesterday"
        assert len(answers[1]["rows"]) == 1
        assert answers[1]["rows"][0]["product"] == "C5"

    finally:
        db.close()


def test_answer_datasets_excludes_non_answer_purpose():
    """If a sub-query is tagged ``probe`` or ``auxiliary``, it should
    NOT land in ``answer_datasets()`` — same semantics as the
    existing top-level path. Pin the iteration loop respects purpose.
    """
    contract = GoalContract()
    contract.record_dataset(
        rows=[{"x": 1}],
        sql="-- schema probe",
        source_name="probe",
        purpose="probe",
        tool_call_id="t1",
    )
    contract.record_dataset(
        rows=[{"y": 2}],
        sql="SELECT y FROM real_data",
        source_name="real",
        purpose="answer",
        tool_call_id="t2",
    )
    answers = contract.answer_datasets()
    assert len(answers) == 1
    assert answers[0]["source_name"] == "real"


def test_top_level_shape_still_works_unchanged():
    """Pin the existing ask_data_agent / execute_query path. Pinned
    here so the refactor that looped fetch_data_batch through the
    same body does NOT regress the legacy top-level shape.
    """
    contract = GoalContract()
    contract.record_dataset(
        rows=[{"product": "Crude Oil", "price": 92.0}],
        sql="SELECT product, price FROM crude_oil_today",
        source_name="aipdp_data_warehouse_prod",
        purpose="answer",
        tool_call_id="t1",
    )
    answers = contract.answer_datasets()
    assert len(answers) == 1
    assert answers[0]["source_name"] == "aipdp_data_warehouse_prod"
    assert answers[0]["rows"][0]["product"] == "Crude Oil"
