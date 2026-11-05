"""P2 Phase 2 — locally-runnable CORE tests (no app.models import needed).

These exercise the modules that do NOT pull in app.models, so they run in any
environment (the local anaconda SQLAlchemy build is currently missing
sqlalchemy/inspect.py, which breaks app.models). The full integration suite
(test_p2_phase2_collaboration.py: delegate_task review, DAG reviewer/conditional/
approval nodes, scenarios A/E/F) runs in the container where SQLAlchemy is intact.

Covered here:
  * shared_evidence registry (store / get / find-by-topic / per-conversation
    isolation / reference-not-raw).
  * reviewer fact-safety (structured verdict, unsupported-claim detection,
    conservative on parse failure — never approves invented content).
  * broker governance for conditional peer collaboration (duplicate blocked,
    circular blocked, nested peer allowed).
  * evidence_tools provide/request by reference.
"""
import asyncio
from unittest.mock import MagicMock, patch

from app.services.agent_collaboration import broker  # noqa: E402
from app.services.agent_collaboration import shared_evidence as se  # noqa: E402
from app.services.agent_collaboration import reviewer as rv  # noqa: E402
from app.services.agent_collaboration import evidence_tools  # noqa: E402


def _reset():
    se.reset_registry()
    broker.reset_concurrency()
    broker.reset_conv_state()
    from app.services.agent_collaboration.governance_store import reset_store
    reset_store()


def _fake_db_published():
    """MagicMock db whose AgentApp query returns a published app for any name."""
    db = MagicMock()
    app = MagicMock()
    app.status = "published"
    app.org_id = "default-org"
    q = db.query.return_value
    q.filter.return_value.first.return_value = app
    return db


# ── shared evidence ──────────────────────────────────────────────────────

def test_evidence_store_get_list_find():
    _reset()
    eid = se.provide_evidence(
        "conv-1", "Inventory Management Agent", source_id="ds-1",
        findings="safety stock = 120 units", assumptions=["lead time 5d"],
        validation_status=se.VALIDATION_VALIDATED)
    item = se.get_evidence("conv-1", eid)
    assert item.findings == "safety stock = 120 units"
    assert item.validation_status == se.VALIDATION_VALIDATED
    refs = se.request_evidence_references("conv-1", "safety stock")
    assert len(refs) == 1 and refs[0]["evidence_id"] == eid
    assert se.request_evidence_references("conv-1", "zzz-nope") == []


def test_evidence_isolation_per_conversation():
    _reset()
    se.provide_evidence("cA", "AgentA", findings="only in A")
    assert se.request_evidence_references("cB", "only in A") == []


def test_evidence_request_returns_reference_not_raw():
    _reset()
    eid = se.provide_evidence(
        "conv-2", "Sales Agent", source_id="ds-2",
        findings={"units": 5000, "revenue": 1_000_000})
    refs = se.request_evidence_references("conv-2", "units")
    assert refs[0]["evidence_id"] == eid
    assert "findings_summary" in refs[0]
    assert "findings" not in refs[0]  # no raw payload between agents


# ── evidence tools ──────────────────────────────────────────────────────

def test_evidence_tools_provide_request_by_reference():
    _reset()
    ctx = {"conversation_id": "c-tool", "agent_name": "Inventory Management Agent"}
    res = asyncio.run(evidence_tools.provide_evidence_tool(
        {"findings": "shortage SKU-9 40 units", "source_id": "ds-inv",
         "validation_status": se.VALIDATION_VALIDATED}, None, None, ctx))
    assert res["success"] is True and res["evidence_id"]
    req = asyncio.run(evidence_tools.request_evidence_tool(
        {"topic": "SKU-9"}, None, None, {"conversation_id": "c-tool"}))
    assert req["count"] == 1
    assert req["references"][0]["producer_agent"] == "Inventory Management Agent"


# ── reviewer fact-safety ────────────────────────────────────────────────

def _patch_reviewer(payload):
    return patch.object(rv, "chat_completion_json_sync", return_value=payload)


def test_reviewer_structured_verdict():
    _reset()
    payload = {"dimensions": {d: {"status": "ok", "detail": ""} for d in rv.DIMENSIONS},
               "overall": "approved", "answered": True, "issues": [], "needs_evidence": []}
    with _patch_reviewer(payload):
        rep = rv.review_findings("q", "answer", evidence_items=[])
    assert rep.overall == "approved" and rep.answered is True
    assert all(rep.dimensions[d]["status"] == "ok" for d in rv.DIMENSIONS)


def test_reviewer_detects_unsupported_claim():
    _reset()
    dims = {d: {"status": "ok", "detail": ""} for d in rv.DIMENSIONS}
    dims["unsupported_conclusions"] = {"status": "fail", "detail": "claimed X w/o evidence"}
    dims["contradictory_findings"] = {"status": "fail", "detail": "conflict"}
    payload = {"dimensions": dims, "overall": "needs_revision", "answered": True,
               "issues": ["unsupported claim X"], "needs_evidence": ["measure X"]}
    with _patch_reviewer(payload):
        rep = rv.review_findings("q", "answer", evidence_items=[])
    assert rep.overall == "needs_revision"
    assert rep.dimensions["unsupported_conclusions"]["status"] == "fail"


def test_reviewer_never_invents_on_parse_failure():
    _reset()
    with _patch_reviewer({}):
        rep = rv.review_findings("q", "answer", evidence_items=[])
    assert rep.overall == "needs_revision"  # conservative, never approves fake


# ── broker governance: conditional peer collaboration (Part 2) ──────────

def test_duplicate_delegation_blocked():
    _reset()
    ctx = {"conversation_id": "c-g", "agent_name": "general_assistant"}
    db = _fake_db_published()
    ok1, _, _ = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="count stock",
        context=ctx, db=db)
    assert ok1 is True
    ok2, reason2, _ = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="count stock",
        context=ctx, db=db)
    assert ok2 is False and "duplicate" in (reason2 or "").lower()


def test_circular_delegation_blocked():
    _reset()
    ctx = {"conversation_id": "c-g2", "agent_name": "general_assistant",
           "delegation_path": ["Supply Chain Agent"]}
    db = _fake_db_published()
    ok, reason, _ = broker.evaluate_delegation(
        target_agent_id="Supply Chain Agent", objective="loop", context=ctx, db=db)
    assert ok is False and "cycle" in (reason or "").lower()


def test_nested_peer_collaboration_allowed():
    _reset()
    ctx = {"conversation_id": "c-g3", "agent_name": "general_assistant",
           "delegation_path": ["Supply Chain Agent"]}
    db = _fake_db_published()
    ok, _, ext = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="verify shortage",
        context=ctx, db=db)
    assert ok is True
    assert "Inventory Management Agent" in ext["delegation_path"]
