"""Regression: ``describe_schema(table=X)`` must describe table X.

Live defect (2026-09-18), captured from a real turn's persisted trace:

    describe_schema table=erp_t_crm_contract        -> md5 d5ca98537ab1, 6825 chars
    describe_schema table=erp_t_crm_contractentry   -> md5 d5ca98537ab1, 6825 chars
    describe_schema table=erp_product_sales_details -> md5 d5ca98537ab1, 6825 chars

Byte-identical, every result carrying ``source=schema_graph``, and NONE of the
three requested tables appeared anywhere in the returned text — the payload was
the semantic linker's question-driven slice (decision_log, insight_market_cache,
…).

Root cause: the linker/graph branch in ``_describe_schema`` ran whenever schema
linking was enabled, and it derives its tables from a *question* — the ``table``
argument was never consulted.  Both of that branch's early returns (the
SchemaGraph text and the catalog slice) therefore swallowed the explicit table.

Consequence in production: the agent could not learn the columns of the table it
had named, guessed alternatives, and the Data Agent failed 6 times in a single
turn before the run gave up with "I wasn't able to finish that in one pass".

The fix gates that branch on ``not table`` so a named table is answered by
``SchemaService.describe_table`` — and these tests pin both halves: the named
table is honoured, and the question-driven linker is still used when no table
is named.
"""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: F401,E402  (register all models on Base)
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.tool_handlers import db_tools  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _seed_kb(db, name="sales_db"):
    kb = KnowledgeBase(name=name, source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app")
    db.add(kb)
    db.commit()
    return kb


class _FakeSchemaService:
    """Stands in for the live introspection: returns per-table columns."""

    calls: list[tuple[str, str]] = []

    def __init__(self, db):
        pass

    def describe_table(self, kb_id, table):
        _FakeSchemaService.calls.append((kb_id, table))
        return {
            "table": table,
            "columns": [f"{table}_col_a", f"{table}_col_b"],
            "row_count": 7,
        }

    def describe_all(self, kb_id, max_tables=30):
        return {"tables": ["one", "two"]}

    def list_tables(self, kb_id):
        return {"tables": ["one", "two"]}


def _ctx(kb_id, **extra):
    ctx = {
        "bound_kb_ids": [kb_id],
        # what the Data Agent sub-context sets — this is what made the
        # linker/graph branch fire in production
        "schema_linking_enabled": True,
        "schema_graph_enabled": True,
        "schema_validator_enabled": True,
    }
    ctx.update(extra)
    return ctx


def _patch_common(monkeypatch, linker_calls):
    async def fake_link_schema(**kw):
        linker_calls.append(kw)
        return {
            "tables": [{"table_name": "decision_log"}],
            "slice_text": "Table: decision_log\n  columns:\n    - id\n",
        }

    monkeypatch.setattr(
        "app.services.knowledge_graph.schema_linker.link_schema", fake_link_schema
    )
    monkeypatch.setattr(db_tools, "SchemaService", _FakeSchemaService)
    monkeypatch.setattr(db_tools, "connection_fingerprint", lambda db, kb_id: "")
    monkeypatch.setattr(db_tools, "_apply_schema_policy", lambda r, p, k, t: r)
    monkeypatch.setattr(db_tools, "_annotate_table_roles", lambda r, d, k: r)


def test_named_table_is_described_not_replaced_by_the_linker(monkeypatch, db):
    """The core regression: two different named tables must return two
    different, table-specific schemas — and the linker must not run at all."""
    _FakeSchemaService.calls = []
    linker_calls: list[dict] = []
    _patch_common(monkeypatch, linker_calls)

    user = _seed_user(db)
    kb = _seed_kb(db)
    ctx = _ctx(kb.id)

    a = _run(db_tools._describe_schema(
        {"data_source_id": kb.id, "table": "erp_t_crm_contract"}, db, user.id, ctx))
    b = _run(db_tools._describe_schema(
        {"data_source_id": kb.id, "table": "erp_product_sales_details"}, db, user.id, ctx))

    assert a["success"] is True and b["success"] is True
    # the requested table's own columns come back (SchemaService.describe_table
    # returns a structured {table, columns} result — no 'schema' string)
    assert "erp_t_crm_contract_col_a" in str(a)
    assert "erp_product_sales_details_col_a" in str(b)
    # and the two answers are NOT the same blob (this also catches a schema
    # cache whose key omitted the table)
    assert str(a) != str(b)
    # the linker must never be consulted for a named table
    assert linker_calls == [], f"linker ran for a named table: {linker_calls}"
    assert [t for (_kb, t) in _FakeSchemaService.calls] == [
        "erp_t_crm_contract",
        "erp_product_sales_details",
    ]


def test_result_is_not_the_linker_source_for_a_named_table(monkeypatch, db):
    """Belt and braces on the exact production symptom: ``source`` must not be
    the linker/graph one when a table was named."""
    _FakeSchemaService.calls = []
    linker_calls: list[dict] = []
    _patch_common(monkeypatch, linker_calls)

    user = _seed_user(db)
    kb = _seed_kb(db)

    r = _run(db_tools._describe_schema(
        {"data_source_id": kb.id, "table": "erp_t_sal_outstock"}, db, user.id, _ctx(kb.id)))

    assert r.get("source") not in ("schema_graph", "catalog")
    assert "decision_log" not in str(r.get("schema", ""))


def test_linker_still_used_when_no_table_is_named(monkeypatch, db):
    """The fix must not disable the question-driven linker for its actual
    purpose: describing a data source without naming a table.  With the graph
    enabled (as the Data Agent sub-context does) the linker's candidates go
    through SchemaGraph, so ``source`` is 'schema_graph'."""
    _FakeSchemaService.calls = []
    linker_calls: list[dict] = []
    _patch_common(monkeypatch, linker_calls)

    user = _seed_user(db)
    kb = _seed_kb(db)

    r = _run(db_tools._describe_schema(
        {"data_source_id": kb.id, "question": "这个月的合同执行情况"}, db, user.id, _ctx(kb.id)))

    assert len(linker_calls) == 1, "linker should run when no table is named"
    assert r.get("source") in ("schema_graph", "catalog")
    # the per-table introspection path must NOT have been used
    assert _FakeSchemaService.calls == []


def test_named_table_ignores_the_question_anchor(monkeypatch, db):
    """Even with a question present, an explicit table wins — otherwise the
    same turn's later calls keep getting the first question's slice."""
    _FakeSchemaService.calls = []
    linker_calls: list[dict] = []
    _patch_common(monkeypatch, linker_calls)

    user = _seed_user(db)
    kb = _seed_kb(db)

    r = _run(db_tools._describe_schema(
        {"data_source_id": kb.id, "table": "erp_paez_t_monweek_plan",
         "question": "这个月的合同执行情况"},
        db, user.id, _ctx(kb.id)))

    assert linker_calls == []
    assert "erp_paez_t_monweek_plan_col_a" in str(r)
