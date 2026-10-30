"""P5-T1 — MetricDefinition as the ONE authoritative metric registry.

Adds the governance fields the acceptance review requires (dimensions,
time semantics, currency, zero-denominator behavior, structured filters,
evidence status, version, lifecycle, reviewer) and makes lifecycle real:
draft and retired metrics are excluded from the ENFORCEMENT read
(datasource_metric_bindings) — a retired or not-yet-approved metric must
never constrain or resolve NL2SQL queries.

Additive migration only (111): nullable columns + server defaults; the
existing enforcement pipeline keeps working unchanged.
"""

from __future__ import annotations

import uuid

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition

ORG = "org-mgo"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        eng,
        tables=[KnowledgeBase.__table__, MetricDefinition.__table__],
    )
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _kb(db, kb_id="kb-mgo-1") -> KnowledgeBase:
    kb = KnowledgeBase(org_id=ORG, name="mgo", source_kind="database",
                       db_type="sqlite", file_url=None)
    kb.id = kb_id
    db.add(kb)
    db.commit()
    return kb


def _metric(db, kb, *, name="gross revenue", lifecycle="published",
            reviewed_by="admin-1", **kw) -> MetricDefinition:
    m = MetricDefinition(
        id=str(uuid.uuid4()), org_id=ORG, datasource_id=kb.id,
        name=name, base_table="sales_order", base_column="famount",
        aggregation="sum", is_enabled=True,
        lifecycle=lifecycle, reviewed_by=reviewed_by, **kw)
    db.add(m)
    db.commit()
    return m


# ── registry extension: defaults ─────────────────────────────────────────

def test_new_registry_fields_have_governed_defaults(db):
    kb = _kb(db)
    m = _metric(db, kb)
    assert m.evidence_status == "verified"
    assert m.version == 1
    assert m.lifecycle == "published"
    assert m.time_column is None
    assert m.dimensions in (None, [])
    assert m.currency is None
    assert m.zero_denominator_behavior is None
    assert m.filters_json in (None, [])
    assert m.source_object_refs in (None, [])


def test_zero_denominator_behavior_uses_ratio_contract_vocabulary(db):
    """No new vocabulary — reuse the ratio contract's (P5 invariant I6)."""
    from app.services.db.ratio_contract import ZERO_DENOMINATOR_BEHAVIORS

    assert set(ZERO_DENOMINATOR_BEHAVIORS) >= {"unsupported", "null", "zero"}
    kb = _kb(db)
    m = _metric(db, kb, zero_denominator_behavior="null")
    assert m.zero_denominator_behavior in ZERO_DENOMINATOR_BEHAVIORS


# ── lifecycle is real on the ENFORCEMENT read ─────────────────────────────

def test_retired_metric_excluded_from_bindings(db):
    from app.services.db.query_contract import datasource_metric_bindings

    kb = _kb(db)
    _metric(db, kb, lifecycle="retired")
    assert datasource_metric_bindings(db, kb.id) == []


def test_draft_metric_excluded_from_bindings(db):
    from app.services.db.query_contract import datasource_metric_bindings

    kb = _kb(db)
    _metric(db, kb, lifecycle="draft")
    assert datasource_metric_bindings(db, kb.id) == []


def test_published_metric_still_enforced(db):
    from app.services.db.query_contract import datasource_metric_bindings

    kb = _kb(db)
    _metric(db, kb)
    bindings = datasource_metric_bindings(db, kb.id)
    assert len(bindings) == 1
    assert bindings[0].term == "gross revenue"


# ── CRUD: version monotonicity + filters op whitelist ─────────────────────

def test_patch_bumps_version(db):
    kb = _kb(db)
    m = _metric(db, kb)
    from app.routers.knowledge_bases import update_kb_metric

    import asyncio

    asyncio.run(update_kb_metric(
        "app", kb.id, m.id, {"unit": "CNY"}, db=db,
        user=SimpleNamespace(org_id=ORG)))
    db.expire_all()
    assert db.get(MetricDefinition, m.id).version == 2
    asyncio.run(update_kb_metric(
        "app", kb.id, m.id, {"unit": "USD"}, db=db,
        user=SimpleNamespace(org_id=ORG)))
    db.expire_all()
    assert db.get(MetricDefinition, m.id).version == 3


def test_filters_op_whitelist_rejected_on_create(db):
    import asyncio

    from fastapi import HTTPException

    from app.routers.knowledge_bases import create_kb_metric

    kb = _kb(db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_kb_metric("app", kb.id, {
            "name": "net revenue",
            "base_table": "sales_order", "base_column": "famount",
            "filters_json": [{"column": "fbillno", "op": "DROP TABLE", "value": 1}],
        }, db=db, user=SimpleNamespace(org_id=ORG)))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        asyncio.run(create_kb_metric("app", kb.id, {
            "name": "net revenue 2", "base_table": "sales_order",
            "base_column": "famount",
            "filters_json": "not-a-list",
        }, db=db, user=SimpleNamespace(org_id=ORG)))


def test_filters_valid_ops_accepted_and_persisted(db):
    import asyncio

    from app.models.metric_definition import MetricDefinition
    from app.routers.knowledge_bases import create_kb_metric

    kb = _kb(db)
    out = asyncio.run(create_kb_metric("app", kb.id, {
        "name": "net revenue",
        "base_table": "sales_order", "base_column": "famount",
        "filters_json": [{"column": "fdocstatus", "op": "eq", "value": "C"}],
    }, db=db, user=SimpleNamespace(org_id=ORG)))
    row = db.get(MetricDefinition, out["id"])
    assert row.filters_json == [
        {"column": "fdocstatus", "op": "eq", "value": "C"}]
    assert row.lifecycle == "published" and row.version == 1
