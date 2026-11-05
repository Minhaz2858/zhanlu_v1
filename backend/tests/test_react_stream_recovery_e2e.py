"""End-to-end ReAct finalization regression (Phase 4 acceptance gate).

Drives the REAL ``POST /apps/{app}/agents/conversations/v3/{conv}/messages/stream``
route through the real ``event_stream`` generator — the LLM boundary
(``_call_llm_with_tools``, ``_call_synthesis_llm``) and the tool-executor
boundary (``execute_tool``) are the only mocks. No real warehouse, no real
LLM, no production state (shared sqlite test engine from conftest).

Incident under test (convs 6c466a7d / 7e2e6a53): successful ``query_database``
calls recorded rows in ``observation_records`` under an execution id carried
in the tool result's ``provenance``; the wall-clock/give-up exit produced the
generic "had trouble putting it all together" apology because the post-loop
synthesizer only consulted goal-contract answer datasets.

Proven here end-to-end:
- recovery hook feeds the recovered rows into ``_force_llm_synthesis`` and the
  terminal ``done`` carries the grounded answer, not the apology;
- a ``content_replace`` frame is emitted (streamed-path contract);
- the tool executor runs exactly once — recovery never re-executes SQL;
- no usable observations → the honest apology is preserved verbatim;
- failed observations and other-tenant observations are excluded.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.deps import get_db, get_current_user_required  # noqa: E402
from app.config import settings  # noqa: E402

QUESTION = "Give me last month's sales report."
APOLOGY = (
    "I gathered some information but had trouble putting it all together. "
    "Could you try again with a more specific request?"
)
FIXTURE_ROWS = [
    {"month": "2026-09", "amount": 148.6},
    {"month": "2026-09", "amount": 32.1},
]
GROUNDED = (
    "Executive Summary: September 2026 sales totaled 180.7 across two "
    "recorded transactions, the largest at 148.6 and the remainder at 32.1.\n\n"
    "Key Numbers: total 180.7; largest transaction 148.6 (82.2% of total); "
    "second transaction 32.1.\n\n"
    "Detailed Breakdown: both figures come directly from the rows returned by "
    "the warehouse for the requested month; no external estimates were used.\n\n"
    "Risks & Opportunities: single-transaction concentration above four "
    "fifths of revenue.\n\n"
    "Recommended Actions: verify whether the low transaction count reflects a "
    "data capture gap before drawing conclusions."
)
TOOL_RESULT_ROWS = [{"month": "2026-09", "amount": 148.6}]


# ── Seeding ─────────────────────────────────────────────────────────────────


def _seed_env(db, *, org_id="org-e2e", success_obs=True, failed_obs=False,
              execution_org_id=None):
    """Seed admin user, agent app, conversation and execution/observations.

    Returns (user, app, conv, execution_id).
    """
    from app.models.user import User
    from app.models.agent_app import AgentApp
    from app.models.agent_conversation import AgentConversation
    from app.models.execution import Execution, ObservationRecord

    user = User(
        email=f"e2e-{uuid.uuid4().hex[:8]}@test.local",
        full_name="E2E Admin",
        role="admin",
        password_hash="x",
        org_id=org_id,
        app_id="app-e2e",
    )
    db.add(user)
    db.flush()

    app_row = AgentApp(
        id=f"app-{uuid.uuid4().hex[:12]}",
        name="general_assistant",
        org_id=org_id,
        app_id="app-e2e",
        status="published",
        capabilities=["query_database"],
    )
    db.add(app_row)
    db.flush()

    conv = AgentConversation(
        id=str(uuid.uuid4()),
        agent_name=app_row.name,
        org_id=org_id,
        app_id="app-e2e",
        created_by_id=user.id,
        title="phase4-e2e",
        messages=[],
    )
    db.add(conv)
    db.flush()

    ex = Execution(
        id=str(uuid.uuid4()),
        conversation_id=conv.id,
        org_id=execution_org_id or org_id,
        app_id="app-e2e",
        agent_name="general_assistant",
        user_message=QUESTION,
    )
    db.add(ex)
    db.flush()

    if success_obs:
        db.add(ObservationRecord(
            id=str(uuid.uuid4()), execution_id=ex.id, seq=1,
            observation_type="nl2sql", tool_name="nl2sql", success=True,
            result_data={
                "sql": "SELECT month, amount FROM erp_v_sale_orderentry",
                "data": FIXTURE_ROWS,
                "source_id": "kb-1",
                "source_name": "aipdp_data_warehouse_prod",
            },
        ))
        db.add(ObservationRecord(
            id=str(uuid.uuid4()), execution_id=ex.id, seq=2,
            observation_type="nl2sql", tool_name="nl2sql", success=True,
            result_data={
                "sql": "SELECT month, amount FROM erp_v_sale_orderentry",
                "data": FIXTURE_ROWS,
                "source_id": "kb-1",
                "source_name": "aipdp_data_warehouse_prod",
            },
        ))
    if failed_obs:
        db.add(ObservationRecord(
            id=str(uuid.uuid4()), execution_id=ex.id, seq=3,
            observation_type="nl2sql", tool_name="nl2sql", success=False,
            result_data={"sql": "SELECT month, amount FROM other_tenant_sales",
                         "data": [{"month": "2026-09", "amount": 99999.0}],
                         "source_id": "kb-1",
                         "source_name": "aipdp_data_warehouse_prod"},
            error_message="column 'amount' not found",
        ))
    db.commit()
    return user, app_row, conv, ex.id


def _harness(db, monkeypatch, user, conv_id, llm_script, *, wall_cap=False):
    """Build the TestClient with real router + boundary mocks.

    llm_script: list of responses for successive _call_llm_with_tools calls.
    Returns (client, calls) where calls records executor/synthesis activity.
    """
    import app.routers.agents as agents_mod

    monkeypatch.setattr(agents_mod, "STREAM_TOKEN_DELTAS", False)
    monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")

    llm_calls = {"n": 0}

    async def fake_llm(messages, tools, tool_choice=None, **kw):
        idx = min(llm_calls["n"], len(llm_script) - 1)
        llm_calls["n"] += 1
        resp = llm_script[idx]
        return {"content": resp["content"], "tool_calls": resp.get("tool_calls", [])}

    executor_calls = {"n": 0}

    async def fake_execute_tool(name, args, session, user_id, context=None):
        executor_calls["n"] += 1
        return {
            "success": True,
            "rows": TOOL_RESULT_ROWS if success_ctx["has_rows"] else [],
            "sql": "SELECT month, amount FROM erp_v_sale_orderentry",
            "source_id": "kb-1",
            "source_name": "aipdp_data_warehouse_prod",
            "provenance": {"execution_id": success_ctx["execution_id"]},
        }

    synth_calls = {"n": 0, "prompts": []}

    async def fake_synth(*args, **kwargs):
        synth_calls["n"] += 1
        synth_calls["prompts"].append(str(args[0] if args else kwargs))
        return {"content": GROUNDED}

    monkeypatch.setattr(agents_mod, "_call_llm_with_tools", fake_llm)
    monkeypatch.setattr(agents_mod, "_call_synthesis_llm", fake_synth)
    monkeypatch.setattr(agents_mod, "execute_tool", fake_execute_tool)

    if wall_cap:
        # Force the REAL wall-clock branch: tier target ~0 → the cap fires at
        # the end of iteration 1; disable extensions so it cannot buy time.
        monkeypatch.setattr(settings, "TURN_WALL_TARGET_STANDARD_S", 0.05)
        monkeypatch.setattr(settings, "TURN_WALL_MAX_EXTENSIONS", 0)
        monkeypatch.setattr(settings, "TURN_COMPLEXITY_ENABLED", False)

    def _override_db():
        yield db

    def _override_user():
        return success_ctx["user"]

    api = FastAPI()
    api.include_router(agents_mod.router)
    api.dependency_overrides[get_db] = _override_db
    api.dependency_overrides[get_current_user_required] = _override_user

    frames = []

    with TestClient(api) as client:
        with client.stream(
            "POST",
            f"/apps/app-e2e/agents/conversations/v3/{conv_id}/messages/stream",
            json={"content": QUESTION},
        ) as resp:
            assert resp.status_code == 200, resp.read()[:500]
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    try:
                        frames.append(json.loads(line[len("data: "):]))
                    except json.JSONDecodeError:
                        pass
    return frames, {"llm": llm_calls, "executor": executor_calls, "synth": synth_calls}


# The executor/seed context for the current test (closure-friendly).
success_ctx = {"execution_id": None, "user": None, "has_rows": True}


def _run_case(monkeypatch, *, success_obs=True, failed_obs=False,
              execution_org_id=None, llm_script=None, wall_cap=False,
              has_rows=True):
    db = SessionLocal()
    try:
        user, app_row, conv, ex_id = _seed_env(
            db, success_obs=success_obs, failed_obs=failed_obs,
            execution_org_id=execution_org_id,
        )
        success_ctx.update(execution_id=ex_id, user=user, has_rows=has_rows)
        script = llm_script or [
            {"content": "", "tool_calls": [{
                "id": "call_1", "type": "function", "function": {
                    "name": "query_database",
                    "arguments": json.dumps({"question": "sales last month"}),
                }}]},
            {"content": APOLOGY, "tool_calls": []},
        ]
        frames, calls = _harness(
            db, monkeypatch, user, conv.id, script, wall_cap=wall_cap)
        done = next((f for f in frames if f.get("type") == "done"), None)
        return frames, done, calls, db
    finally:
        db.close()


# ── Recovery success case (the incident, end to end) ────────────────────────


def test_recovery_feeds_grounded_answer_to_done(monkeypatch):
    frames, done, calls, _db = _run_case(monkeypatch)
    assert done is not None, frames[-3:]
    content = done.get("content") or ""
    assert "had trouble" not in content
    assert "180.7" in content          # fixture-grounded, from recovered rows
    assert "148.6" in content
    # The real synthesis handoff ran exactly once with the recovered rows.
    assert calls["synth"]["n"] >= 1
    assert any("148.6" in p for p in calls["synth"]["prompts"]), \
        "recovered rows must reach the synthesis prompt"
    # Tool executor ran exactly once — recovery must not re-execute SQL.
    assert calls["executor"]["n"] == 1
    # Terminal correctness (Chat.doneAuthoritative contract): done.content is
    # the authoritative final answer for the client — mid-stream transient
    # deltas (including the model's own apology) are overwritten by it. The
    # terminal answer above is therefore the user-visible final content.
    #
    # Persisted consistency: the backend row must carry the same grounded
    # answer the done frame delivered.
    db = SessionLocal()
    try:
        from app.models.agent_conversation import AgentConversation
        conv_row = db.get(AgentConversation, done["conversation"]["id"])
        persisted = (conv_row.messages or [{}])[-1].get("content") or ""
        assert persisted == (done.get("content") or ""), (
            persisted[:120], (done.get("content") or "")[:120])
        assert "had trouble" not in persisted
    finally:
        db.close()


def test_wall_cap_break_enters_same_finalization(monkeypatch):
    """Forced wall-clock condition (tier target ≈ 0, extensions 0): the loop
    breaks after iteration 1 and the SAME post-loop finalization must ground
    the answer from recovered rows — without a second tool execution."""
    frames, done, calls, _db = _run_case(monkeypatch, wall_cap=True)
    assert done is not None, frames[-3:]
    content = done.get("content") or ""
    assert "had trouble" not in content
    assert "180.7" in content
    assert calls["executor"]["n"] == 1
    assert any("148.6" in p for p in calls["synth"]["prompts"])


# ── No-data case: honest failure preserved ──────────────────────────────────


def test_no_usable_observations_keeps_honest_failure(monkeypatch):
    frames, done, calls, _db = _run_case(
        monkeypatch, success_obs=False, has_rows=False)
    assert done is not None
    content = done.get("content") or ""
    assert "had trouble" in content          # existing honest fallback intact
    assert "180.7" not in content and "148.6" not in content  # nothing invented
    assert calls["executor"]["n"] == 1       # exactly the model's own call


# ── Failed-observation case: excluded from synthesis ────────────────────────


def test_failed_observations_excluded_from_synthesis(monkeypatch):
    frames, done, calls, _db = _run_case(monkeypatch, success_obs=False,
                                         failed_obs=True, has_rows=False)
    assert done is not None
    content = done.get("content") or ""
    # The failed observation queried a different (99999.0) figure — it must
    # never reach the answer.
    assert "99999" not in content
    assert "had trouble" in content          # honest failure (nothing usable)


# ── Isolation case: other-tenant observation cannot enter synthesis ────────


def test_other_tenant_observation_excluded(monkeypatch):
    frames, done, calls, _db = _run_case(
        monkeypatch, success_obs=False, failed_obs=False,
        execution_org_id="org-OTHER", has_rows=False)
    assert done is not None
    content = done.get("content") or ""
    assert "148.6" not in content and "180.7" not in content
    assert "had trouble" in content
