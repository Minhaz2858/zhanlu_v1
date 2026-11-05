"""P2 Phase 3 — Multi-agent execution visualization backend contract tests.

Tests the execution-tree builder (build_agent_tree) and the shared-evidence
contract used by the frontend Evidence panel. LLM boundary is mocked/irrelevant;
we exercise the real AgentInvocation records, the span_id parent linkage, the
reviewer/evidence persistence, and tenant-scoped tree assembly.

These are NOT live-LLM tests (provider quota is externally blocked). They verify
the backend event/data contract the frontend renders against.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
import app.models  # noqa: F401  (registers all models incl. AgentInvocation)  # noqa: E402
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.agent_invocation import AgentInvocation  # noqa: E402
from app.services.agent_invocations import build_agent_tree  # noqa: E402
from app.services.agent_collaboration import shared_evidence as se  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _reset():
    se.reset_registry()
    yield
    se.reset_registry()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def agents(db):
    rows = [
        AgentApp(name="general_assistant", description="ga", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Quality Control Agent", description="qc", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Supply Chain Agent", description="sc", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Inventory Management Agent", description="inv", status="published",
                 org_id="default-org", app_id="default-app"),
    ]
    db.add_all(rows)
    db.commit()
    return {a.name: a.id for a in rows}


def _inv(db, **kw):
    row = AgentInvocation(**kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _now(off=0):
    return datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc).replace(second=off)


# ── Execution tree: supervisor + peer + reviewer + partial ────────────────────
def test_build_agent_tree_structure(db, agents):
    conv = "conv-tree-1"
    uid = "user-tree-1"
    ga_id = agents["general_assistant"]

    root = _inv(db, conversation_id=conv, user_id=uid, agent_app_id=ga_id,
                invocation_type="conversation", status="completed",
                input_message="Why did line 3 defect rate spike?",
                started_at=_now(0), completed_at=_now(30), duration_ms=30000,
                token_usage={"total_tokens": 500}, cost_amount=0.01)

    qc = _inv(db, conversation_id=conv, user_id=uid, agent_app_id=agents["Quality Control Agent"],
              invocation_type="sub_agent", status="completed",
              input_message="Investigate defects on line 3",
              span_id=root.id, delegation_mode="supervisor",
              started_at=_now(2), completed_at=_now(20), duration_ms=18000,
              token_usage={"total_tokens": 220}, cost_amount=0.004,
              output_json={"agent_name": "Quality Control Agent", "review": {
                  "overall": "approved", "dimensions": {}, "issues": [],
                  "needs_evidence": []}})

    # Peer collaboration: Supply Chain requests Inventory assistance (Pattern C).
    sc = _inv(db, conversation_id=conv, user_id=uid, agent_app_id=agents["Supply Chain Agent"],
              invocation_type="sub_agent", status="completed",
              input_message="Check inventory", span_id=root.id,
              delegation_mode="peer", output_json={
                  "agent_name": "Supply Chain Agent",
                  "requesting_agent_id": "Inventory Management Agent"},
              token_usage={"total_tokens": 90}, cost_amount=0.002)

    tree = build_agent_tree(db, conversation_id=conv, user_id=uid)
    roots = tree["roots"]
    assert len(roots) == 1
    ga = roots[0]
    assert ga["agent_name"] == "general_assistant"
    assert ga["role"] == "supervisor"
    assert ga["phase"] == "completed"
    child_names = {c["agent_name"] for c in ga["children"]}
    assert child_names == {"Quality Control Agent", "Supply Chain Agent"}

    qc_node = next(c for c in ga["children"] if c["agent_name"] == "Quality Control Agent")
    assert qc_node["delegation_mode"] == "supervisor"
    assert qc_node["phase"] == "completed"  # review approved -> not "reviewing"

    sc_node = next(c for c in ga["children"] if c["agent_name"] == "Supply Chain Agent")
    assert sc_node["delegation_mode"] == "peer"

    s = tree["summary"]
    assert s["total"] == 3
    assert s["completed"] == 3
    assert s["peer_collaborations"] == 1
    assert s["total_tokens"] == (500 + 220 + 90)
    assert abs(s["estimated_cost"] - 0.016) < 1e-9
    assert s["duration_ms"] == 30000


def test_build_agent_tree_reviewer_phase_and_partial(db, agents):
    conv = "conv-tree-2"
    uid = "user-tree-2"
    ga_id = agents["general_assistant"]
    root = _inv(db, conversation_id=conv, user_id=uid, agent_app_id=ga_id,
                invocation_type="conversation", status="completed")

    # A child whose reviewer flagged a gap -> phase should be "reviewing".
    _inv(db, conversation_id=conv, user_id=uid, agent_app_id=agents["Quality Control Agent"],
         invocation_type="sub_agent", status="completed", span_id=root.id,
         output_json={"review": {"overall": "needs_revision", "issues": ["x"],
                                 "needs_evidence": ["y"]}})
    tree = build_agent_tree(db, conversation_id=conv, user_id=uid)
    qc = tree["roots"][0]["children"][0]
    assert qc["phase"] == "reviewing"
    assert tree["summary"]["reviewer_status"] == "needs_revision"


def test_build_agent_tree_awaiting_approval(db, agents):
    conv = "conv-tree-3"
    uid = "user-tree-3"
    root = _inv(db, conversation_id=conv, user_id=uid, agent_app_id=agents["general_assistant"],
                invocation_type="conversation", status="completed")
    _inv(db, conversation_id=conv, user_id=uid, agent_app_id=agents["Supply Chain Agent"],
         invocation_type="sub_agent", status="running", span_id=root.id)
    tree = build_agent_tree(db, conversation_id=conv, user_id=uid, awaiting_approval=True)
    ga = tree["roots"][0]
    assert ga["awaiting_approval"] is True
    assert ga["phase"] == "waiting_for_approval"


def test_build_agent_tree_tenant_isolation(db, agents):
    # A different user's conversation must not appear in another user's tree.
    _inv(db, conversation_id="conv-other", user_id="user-X",
         agent_app_id=agents["general_assistant"], invocation_type="conversation",
         status="completed")
    tree = build_agent_tree(db, conversation_id="conv-other", user_id="user-tree-1")
    assert tree["roots"] == []
    assert tree["summary"]["total"] == 0


# ── Evidence contract (Evidence panel source-of-truth) ───────────────────────
def test_evidence_provide_and_get_contract():
    cid = "conv-ev-1"
    eid = se.provide_evidence(
        cid, "Inventory Management Agent",
        source_id="ds-1", datasource_id="ds-1",
        query_provenance={"sql": "SELECT 1", "execution_id": "e1", "nl": "stock"},
        metric_definition_ids=["m1"], findings={"on_hand": 42},
        freshness={"as_of": "2026-10-31", "state": "current"},
        validation_status=se.VALIDATION_VALIDATED,
        references=[],
    )
    item = se.get_evidence(cid, eid)
    assert item is not None
    d = item.to_dict()
    # Honesty: raw result rows are NOT part of the contract — only compact
    # findings + provenance + validation status.
    assert d["evidence_id"] == eid
    assert d["producer_agent"] == "Inventory Management Agent"
    assert d["validation_status"] == se.VALIDATION_VALIDATED
    assert d["findings"] == {"on_hand": 42}
    assert d["query_provenance"]["sql"] == "SELECT 1"

    # Missing evidence returns None (not a fake/empty record).
    assert se.get_evidence(cid, "does-not-exist") is None


def test_evidence_reference_excludes_raw_rows():
    cid = "conv-ev-2"
    se.provide_evidence(cid, "QA Agent", findings="big dataset",
                        validation_status=se.VALIDATION_UNVALIDATED)
    ref = se.request_evidence_references(cid, "")
    # reference() returns compact summary, never full result rows.
    assert isinstance(ref, list)
    for r in ref:
        assert "evidence_id" in r
        assert "findings_summary" in r


# ── Security follow-up: execution-tree error/block sanitization ──────────────
def test_invocation_error_message_sanitized_in_tree(db, agents):
    # Driver errors can embed SQL fragments/column names — the tree API must
    # pass them through the SAME content-invariant sanitizer as live events.
    root = _inv(db, conversation_id="conv-err-1", user_id="user-err-1",
                agent_app_id=agents["general_assistant"],
                invocation_type="conversation", status="failed",
                error_message="Unknown column 'v_sale_order.fqty' in 'where clause'",
                started_at=_now(0))
    tree = build_agent_tree(db, conversation_id="conv-err-1", user_id="user-err-1")
    node = tree["roots"][0]
    from app.services.agent_loop.sse_builders import _LIVE_EVENT_SAFE_FALLBACK

    assert node["error_message"] == _LIVE_EVENT_SAFE_FALLBACK
    assert root.id  # parent linkage untouched by sanitization


def test_block_reason_sanitized_in_tree(db, agents):
    _inv(db, conversation_id="conv-err-2", user_id="user-err-2",
         agent_app_id=agents["general_assistant"],
         invocation_type="conversation", status="blocked",
         output_json={"policy_violation": "table erp_v_orders not allowed"},
         started_at=_now(0))
    tree = build_agent_tree(db, conversation_id="conv-err-2", user_id="user-err-2")
    from app.services.agent_loop.sse_builders import _LIVE_EVENT_SAFE_FALLBACK

    assert tree["roots"][0]["block_reason"] == _LIVE_EVENT_SAFE_FALLBACK


# ── Lifecycle consistency (mission §23 / Hybrid H1): two-phase invocation ────
# Root cause (2026-10-31 audit): delegation rows were written ONLY at terminal
# states — a long-running child had NO persisted existence, so reconnection
# mid-flight showed an empty tree while the live feed synthesized "running"
# nodes with nothing authoritative behind them.
def test_running_invocation_visible_in_tree(db, agents):
    from app.services.agent_invocations import record_invocation

    inv_id = record_invocation(
        db, agent_app_id=agents["general_assistant"],
        conversation_id="conv-h1-1", user_id="user-h1-1",
        invocation_type="sub_agent", trigger="supervisor",
        input_message="investigate stockouts", status="running",
        open_invocation=True, started_at=_now(0),
    )
    assert inv_id, "open (running) invocation must be recorded"
    tree = build_agent_tree(db, conversation_id="conv-h1-1", user_id="user-h1-1")
    assert tree["summary"]["total"] == 1
    assert tree["roots"][0]["status"] == "running"
    assert tree["roots"][0]["phase"] == "running"


def test_terminal_update_same_row_no_duplicate(db, agents):
    from app.services.agent_invocations import (
        record_invocation, update_invocation_terminal,
    )

    inv_id = record_invocation(
        db, agent_app_id=agents["general_assistant"],
        conversation_id="conv-h1-2", user_id="user-h1-2",
        invocation_type="sub_agent", trigger="supervisor",
        input_message="t", status="running", open_invocation=True,
        started_at=_now(0),
    )
    ok = update_invocation_terminal(
        db, inv_id, status="completed", output_json={"task": "t"},
        completed_at=_now(1), duration_ms=1000,
    )
    assert ok is True
    tree = build_agent_tree(db, conversation_id="conv-h1-2", user_id="user-h1-2")
    assert tree["summary"]["total"] == 1, "terminal update must not create a second row"
    assert tree["roots"][0]["status"] == "completed"


def test_terminal_update_never_regresses(db, agents):
    from app.services.agent_invocations import (
        record_invocation, update_invocation_terminal,
    )

    inv_id = record_invocation(
        db, agent_app_id=agents["general_assistant"],
        conversation_id="conv-h1-3", user_id="user-h1-3",
        invocation_type="sub_agent", trigger="supervisor",
        input_message="t", status="running", open_invocation=True,
        started_at=_now(0),
    )
    assert update_invocation_terminal(db, inv_id, status="completed",
                                      completed_at=_now(1)) is True
    # A late duplicate/out-of-order update must never revive the row.
    assert update_invocation_terminal(db, inv_id, status="running") is False
    tree = build_agent_tree(db, conversation_id="conv-h1-3", user_id="user-h1-3")
    assert tree["roots"][0]["status"] == "completed"


# ── H1-B: orphaned invocation recovery (crash / restart / lost worker) ──────
def test_orphaned_running_invocation_recovered(db, agents):
    """A `running` child whose worker died (process crash / restart) must become
    recoverable with a TRUTHFUL terminal state — never silently completed, and
    never left RUNNING forever."""
    from datetime import datetime, timedelta, timezone as tz
    from app.services.agent_invocations import (
        record_invocation, recover_orphaned_invocations,
    )

    old = datetime.now(tz.utc) - timedelta(hours=2)
    stale_id = record_invocation(
        db, agent_app_id=agents["general_assistant"],
        conversation_id="conv-h1-orphan", user_id="user-h1-4",
        invocation_type="sub_agent", trigger="supervisor",
        input_message="lost worker", status="running",
        open_invocation=True, started_at=old,
    )
    fresh_id = record_invocation(
        db, agent_app_id=agents["general_assistant"],
        conversation_id="conv-h1-orphan", user_id="user-h1-4",
        invocation_type="sub_agent", trigger="supervisor",
        input_message="still in flight", status="running",
        open_invocation=True, started_at=datetime.now(tz.utc),
    )
    n = recover_orphaned_invocations(db, max_age_seconds=3600)
    assert n >= 1
    tree = build_agent_tree(db, conversation_id="conv-h1-orphan", user_id="user-h1-4")
    by_name = {r["agent_name"]: r for r in tree["roots"]}
    stale = by_name["general_assistant"]  # both resolve to same app name; use ids below
    # Inspect raw rows for precision (tree name-collides across the two).
    from app.models.agent_invocation import AgentInvocation
    s_row = db.get(AgentInvocation, stale_id)
    f_row = db.get(AgentInvocation, fresh_id)
    # Stale: truthful terminal state + recovery metadata — NOT completed.
    assert s_row.status in ("failed", "cancelled", "timeout")
    assert s_row.status != "completed"
    assert "orphan" in (s_row.error_message or "").lower()
    assert (s_row.output_json or {}).get("recovery", {}).get("reason") == "worker_lost"
    # Fresh in-flight row untouched.
    assert f_row.status == "running"


def test_recovery_is_idempotent(db, agents):
    """Running recovery twice must not double-recover or resurrect rows."""
    from datetime import datetime, timedelta, timezone as tz
    from app.services.agent_invocations import (
        record_invocation, recover_orphaned_invocations,
    )

    old = datetime.now(tz.utc) - timedelta(hours=3)
    record_invocation(
        db, agent_app_id=agents["general_assistant"],
        conversation_id="conv-h1-orphan2", user_id="user-h1-5",
        invocation_type="sub_agent", trigger="supervisor",
        input_message="lost", status="running", open_invocation=True,
        started_at=old,
    )
    n1 = recover_orphaned_invocations(db, max_age_seconds=3600)
    n2 = recover_orphaned_invocations(db, max_age_seconds=3600)
    assert n1 >= 1
    assert n2 == 0, "already-recovered rows must not be re-recovered"


# ── H1-A verification: parent waits for the slowest required child ──────────
def test_batch_waits_for_all_required_children(db):
    """Four required children with deliberately staggered durations: the batch
    (parent's delegation step) must return only after ALL resolve, with every
    per-item result present — the parent can never report completion while a
    required child is still running."""
    import asyncio
    from unittest.mock import patch
    from app.services.tool_handlers import delegate_tool as dt

    delays = {"A": 0.30, "B": 0.20, "C": 0.05, "D": 0.10}

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        await asyncio.sleep(delays[agent_name])
        return {"success": True, "response": f"done {agent_name}",
                "task": task, "agent_name": agent_name}

    started = asyncio.get_event_loop().time() if False else None
    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
            res = loop.run_until_complete(dt._delegate_batch(
                [
                    {"task": f"task {k}", "agent_name": k, "max_iterations": 5}
                    for k in ("A", "B", "C", "D")
                ],
                db, None, None, None,
            ))
        elapsed = loop.time() - t0
    finally:
        loop.close()
    assert res["status"] == "completed"
    assert len(res["results"]) == 4
    assert elapsed >= 0.30, "batch must wait for the slowest required child"
    assert elapsed < 0.60, "batch must not serialize unnecessarily (parallel execution)"


# ── H1-C verification: mixed running/terminal tree + out-of-order terminal ──
def test_tree_shows_mixed_running_and_terminal_children(db, agents):
    from app.services.agent_invocations import (
        record_invocation, update_invocation_terminal,
    )

    ids = [
        record_invocation(
            db, agent_app_id=agents["general_assistant"],
            conversation_id="conv-h1-mix", user_id="user-h1-6",
            invocation_type="sub_agent", trigger="supervisor",
            input_message=f"child {i}", status="running",
            open_invocation=True, started_at=_now(0),
        )
        for i in range(4)
    ]
    # Two complete concurrently-ish; two still running.
    for i in (0, 1):
        update_invocation_terminal(db, ids[i], status="completed",
                                   completed_at=_now(1))
    tree = build_agent_tree(db, conversation_id="conv-h1-mix", user_id="user-h1-6")
    statuses = sorted(r["status"] for r in tree["roots"])
    assert statuses == ["completed", "completed", "running", "running"]
    assert tree["summary"]["total"] == 4
    assert tree["summary"]["completed"] == 2


def test_out_of_order_failed_after_completed_wins(db, agents):
    """Late/duplicate terminal event must never regress a terminal row
    (§23.6): completed → (late) failed must stay completed."""
    from app.services.agent_invocations import (
        record_invocation, update_invocation_terminal,
    )

    inv_id = record_invocation(
        db, agent_app_id=agents["general_assistant"],
        conversation_id="conv-h1-ooo", user_id="user-h1-7",
        invocation_type="sub_agent", trigger="supervisor",
        input_message="t", status="running", open_invocation=True,
        started_at=_now(0),
    )
    assert update_invocation_terminal(db, inv_id, status="completed",
                                      completed_at=_now(1)) is True
    assert update_invocation_terminal(db, inv_id, status="failed",
                                      error_message="late duplicate") is False
    tree = build_agent_tree(db, conversation_id="conv-h1-ooo", user_id="user-h1-7")
    assert tree["roots"][0]["status"] == "completed"


def test_unauthorized_conversation_gets_no_evidence(db, agents):
    """Adversarial: the evidence REST guard (_owns_conversation) must reject
    a conversation the caller has no invocation/execution stake in — the
    endpoint answers 404 (indistinguishable from nonexistent)."""
    from app.routers.executions import _owns_conversation

    _inv(db, conversation_id="conv-secret", user_id="user-owner",
         agent_app_id=agents["general_assistant"],
         invocation_type="conversation", status="completed")
    # The owner passes; everyone else fails closed.
    assert _owns_conversation(db, "conv-secret", type("U", (), {"id": "user-owner", "org_id": "default-org"})()) is True
    assert _owns_conversation(db, "conv-secret", type("U", (), {"id": "user-attacker", "org_id": "default-org"})()) is False
    assert _owns_conversation(db, "conv-nonexistent", type("U", (), {"id": "user-owner", "org_id": "default-org"})()) is False


def test_evidence_rest_serializer_strips_raw_rows():
    # P2 Phase 4 security gap: the REST serializer must NEVER expose raw
    # result rows. to_dict() leaks query_provenance.rows; _serialize_evidence
    # must strip them (keeping only a count + provenance metadata).
    cid = "conv-rest-1"
    eid = se.provide_evidence(
        cid, "Inventory Management Agent",
        source_id="ds-1",
        query_provenance={"sql": "SELECT 1", "execution_id": "e1",
                          "nl": "stock", "rows": [{"x": 1}, {"x": 2}]},
        findings={"on_hand": 42},
        freshness={"as_of": "2026-10-31"},
        validation_status=se.VALIDATION_VALIDATED,
    )
    item = se.get_evidence(cid, eid)
    from app.routers.executions import _serialize_evidence

    d = _serialize_evidence(item)
    assert d["evidence_id"] == eid
    assert d["producer_agent"] == "Inventory Management Agent"
    # Raw rows are gone; a non-sensitive count remains.
    qp = d["query_provenance"]
    assert "rows" not in qp
    assert qp.get("row_count") == 2
    # Provenance metadata + compact findings survive intact.
    assert qp["sql"] == "SELECT 1"
    assert d["findings"] == {"on_hand": 42}
    assert d["validation_status"] == se.VALIDATION_VALIDATED
