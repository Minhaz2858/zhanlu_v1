"""P3-5 lead scoring tests — grounded rows only, LLM-judged scores with
clamping, honest unscored rows, tool registration."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import OntologyObject

ORG = "org-score"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _mk_object(db, binding="kb:kb1:party_master"):
    obj = OntologyObject(
        id="o-score", org_id=ORG, domain_id="d1", name="Customer",
        backing_kind="table", backing_ref=binding,
        properties_json=[{"name": "name", "type": "string"}],
    )
    db.add(obj)
    db.commit()
    return obj


def _patch_read(monkeypatch, rows):
    import app.services.ontology.data as data_mod

    monkeypatch.setattr(
        data_mod, "read_object_data",
        lambda db_, org_, obj, **kw: {"success": True, "rows": rows})


def _patch_llm(monkeypatch, scores):
    import app.services.llm_service as llm

    monkeypatch.setattr(llm, "chat_completion_json_sync",
                        lambda *a, **kw: {"scores": scores})


def _run(handler, args, db, ctx=None):
    return asyncio.run(handler(args, db, None, ctx or {"org_id": ORG}))


# ── happy paths ──────────────────────────────────────────────────────────

def test_scores_live_rows_sorted_with_reasons(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    _mk_object(db)
    _patch_read(monkeypatch, [
        {"name": "Small Co", "tier": "smb"},
        {"name": "Big Corp", "tier": "enterprise"},
    ])
    _patch_llm(monkeypatch, [
        {"index": 0, "score": 25, "reason": "smb tier"},
        {"index": 1, "score": 92, "reason": "enterprise, high revenue"},
    ])
    out = _run(ot._score_leads, {
        "object_name": "Customer",
        "criteria": ["enterprise segment", "revenue > 1M"],
    }, db)
    assert out["success"]
    top = out["scores"][0]
    assert top["name"] == "Big Corp" and top["lead_score"] == 92
    assert "enterprise" in top["score_reason"]
    assert out["scores"][1]["lead_score"] == 25


def test_scores_clamped_and_invalid_dropped(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    _mk_object(db)
    _patch_read(monkeypatch, [{"name": "a"}, {"name": "b"}])
    _patch_llm(monkeypatch, [
        {"index": 0, "score": 250, "reason": "over"},   # clamps to 100
        {"index": 1, "score": -5, "reason": "under"},   # clamps to 0
        {"index": 9, "score": 50, "reason": "bad idx"}, # dropped
        {"score": 50, "reason": "no idx"},              # dropped
    ])
    out = _run(ot._score_leads, {"object_name": "Customer", "criteria": "any"}, db)
    by_name = {r["name"]: r["lead_score"] for r in out["scores"]}
    assert by_name["a"] == 100 and by_name["b"] == 0


def test_unscored_row_is_none_not_zero(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    _mk_object(db)
    _patch_read(monkeypatch, [{"name": "a"}, {"name": "b"}])
    _patch_llm(monkeypatch, [{"index": 0, "score": 80, "reason": "ok"}])
    out = _run(ot._score_leads, {"object_name": "Customer", "criteria": "x"}, db)
    b = next(r for r in out["scores"] if r["name"] == "b")
    assert b["lead_score"] is None


# ── honesty guards ───────────────────────────────────────────────────────

def test_unbound_object_refuses(db):
    import app.services.tool_handlers.ontology_tools as ot

    _mk_object(db, binding=None)  # manual → not bound to live data
    out = _run(ot._score_leads, {"object_name": "Customer", "criteria": "x"}, db)
    assert not out["success"] and "discovery" in out["reason"]


def test_unknown_object_refuses(db):
    import app.services.tool_handlers.ontology_tools as ot

    out = _run(ot._score_leads, {"object_name": "Nope", "criteria": "x"}, db)
    assert not out["success"]


def test_read_failure_passthrough(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    _mk_object(db)
    import app.services.ontology.data as data_mod
    monkeypatch.setattr(
        data_mod, "read_object_data",
        lambda db_, org_, obj, **kw: {
            "success": False, "reason": "no property mapped"})
    out = _run(ot._score_leads, {"object_name": "Customer", "criteria": "x"}, db)
    assert not out["success"] and "no property mapped" in out["reason"]


def test_llm_failure_is_honest(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    _mk_object(db)
    _patch_read(monkeypatch, [{"name": "a"}])
    import app.services.llm_service as llm

    def boom(*a, **kw):
        raise RuntimeError("provider down")
    monkeypatch.setattr(llm, "chat_completion_json_sync", boom)
    out = _run(ot._score_leads, {"object_name": "Customer", "criteria": "x"}, db)
    assert not out["success"] and "provider down" in out["reason"]


def test_tool_registered():
    import app.services.tool_handlers  # noqa: F401
    from app.services.tool_registry import registry

    assert registry.get_entry("score_leads") is not None
