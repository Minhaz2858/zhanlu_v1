"""FSM tenant-isolation acceptance tests (AT-1..AT-9, 2026-10-31).

Implements the reviewer's acceptance criteria for the FSM stage:

  * Two organizations with IDENTICALLY named agents stay isolated
    (memories, tool configuration, agent configuration).
  * Evaluation profiles cannot cross org boundaries — the unscoped
    name fallback and the app_id-as-PK path are gone.
  * The executions API derives org identity from the AUTHENTICATED
    user; foreign executions are 404 for read/approve/reject/
    observations/replay; replay clones are stamped with the caller's org.
  * Forged identities (org-less, org-mismatched agent_id) fail closed.
  * System-agent-style rows resolve own-org only (Rule-0 parity).

Runs under the T1 contract: module-fresh truncated DB, no concurrent
writers (sequential two-session patterns only).
"""

from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal  # noqa: E402
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.agent_memory import AgentMemory  # noqa: E402
from app.services.synexia.identity import (  # noqa: E402
    memory_write_key,
    resolve_agent_in_org,
)

_ORG_A = f"org-fsm-a-{uuid.uuid4().hex[:8]}"
_ORG_B = f"org-fsm-b-{uuid.uuid4().hex[:8]}"
_NAME = f"fsm_iso_{uuid.uuid4().hex[:6]}"


def _mk_agent(agent_id: str, name: str, org_id: str, **extra) -> AgentApp:
    return AgentApp(
        id=agent_id,
        name=name,
        org_id=org_id,
        app_id="app-fsm",
        project="global",
        description=f"agent-of-{org_id}",
        prompt_identity="",
        prompt_boundary="",
        prompt_reasoning="",
        prompt_tools="",
        prompt_output="",
        capabilities=[],
        skills=[],
        knowledge_bases=[],
        topology="standalone",
        sub_agents=[],
        data_read=True,
        data_write=False,
        status="active",
        is_deleted=False,
        **extra,
    )


def _mk_memory(agent_app_id: str, org_id: str, content: str) -> AgentMemory:
    return AgentMemory(
        agent_app_id=agent_app_id,
        org_id=org_id,
        target="memory",
        content=content,
        char_count=len(content),
    )


@pytest.fixture(scope="module")
def two_org_agents():
    """AT-1: two orgs, identically named agents, distinct configuration,
    memories under each PK, plus a legacy bare-name memory row."""
    db = SessionLocal()
    ids = {
        "agent_a": f"agent-fsm-a-{uuid.uuid4().hex[:8]}",
        "agent_b": f"agent-fsm-b-{uuid.uuid4().hex[:8]}",
        "sys_a": f"agent-fsm-sysa-{uuid.uuid4().hex[:8]}",
        "sys_b": f"agent-fsm-sysb-{uuid.uuid4().hex[:8]}",
    }
    agent_a = _mk_agent(
        ids["agent_a"], _NAME, _ORG_A,
        tool_config={"allow": ["tool-a"]},
        evaluation_profile={"grounding_checks": [{"type": "row_count"}]},
    )
    agent_b = _mk_agent(
        ids["agent_b"], _NAME, _ORG_B,
        tool_config={"allow": ["tool-b"]},
        evaluation_profile={"grounding_checks": [{"type": "sql_only"}]},
    )
    sys_a = _mk_agent(ids["sys_a"], "agent_builder", _ORG_A)
    sys_b = _mk_agent(ids["sys_b"], "agent_builder", _ORG_B)
    db.add_all([agent_a, agent_b, sys_a, sys_b])
    db.add_all([
        _mk_memory(ids["agent_a"], _ORG_A, "MEMORY-OF-ORG-A"),
        _mk_memory(ids["agent_b"], _ORG_B, "MEMORY-OF-ORG-B"),
        # Legacy pre-tenancy shape: keyed by the bare agent name.
        _mk_memory(_NAME, "default-org", "LEGACY-BARE-NAME-MEMORY"),
    ])
    db.commit()
    try:
        yield ids
    finally:
        try:
            db.query(AgentApp).filter(
                AgentApp.id.in_(list(ids.values()))
            ).delete(synchronize_session=False)
            db.query(AgentMemory).filter(
                AgentMemory.content.in_([
                    "MEMORY-OF-ORG-A", "MEMORY-OF-ORG-B",
                    "LEGACY-BARE-NAME-MEMORY",
                ])
            ).delete(synchronize_session=False)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        db.close()


# ── AT-6: identity module fail-closed semantics ───────────────────────────


def test_identity_pk_path_is_org_scoped(two_org_agents):
    """A forged agent_id (org B's PK) resolves NOTHING for org A — and
    vice versa (both directions)."""
    db = SessionLocal()
    try:
        assert resolve_agent_in_org(
            db, agent_id=two_org_agents["agent_b"], org_id=_ORG_A,
        ) is None
        assert resolve_agent_in_org(
            db, agent_id=two_org_agents["agent_a"], org_id=_ORG_B,
        ) is None
        # Same-org PK resolves.
        row = resolve_agent_in_org(
            db, agent_id=two_org_agents["agent_a"], org_id=_ORG_A,
        )
        assert row is not None and row.id == two_org_agents["agent_a"]
    finally:
        db.close()


def test_identity_name_path_is_org_scoped_and_fail_closed(two_org_agents):
    """Identically named agents: each org gets ONLY its own row; an
    org-less request resolves nothing (fail closed)."""
    db = SessionLocal()
    try:
        row_a = resolve_agent_in_org(
            db, agent_name=_NAME, org_id=_ORG_A,
        )
        row_b = resolve_agent_in_org(
            db, agent_name=_NAME, org_id=_ORG_B,
        )
        assert row_a.id == two_org_agents["agent_a"]
        assert row_b.id == two_org_agents["agent_b"]
        assert resolve_agent_in_org(db, agent_name=_NAME, org_id=None) is None
        assert resolve_agent_in_org(db, agent_name=_NAME, org_id="") is None
        # Unknown org → nothing.
        assert resolve_agent_in_org(
            db, agent_name=_NAME, org_id="org-unknown",
        ) is None
    finally:
        db.close()


def test_memory_write_key_fail_closed_for_unknown_org_agent():
    """D-2: an org without its own agent row must NOT write memories
    under a shared key; default-org keeps the legacy bridge."""
    assert memory_write_key(
        agent_row=None, agent_name=_NAME, org_id=_ORG_A,
    ) is None
    assert memory_write_key(
        agent_row=None, agent_name=_NAME, org_id="default-org",
    ) == _NAME


# ── AT-2: memory isolation (reads) ─────────────────────────────────────────


def test_memories_cannot_cross_org_boundaries(two_org_agents):
    from app.services.synexia.context_assembler import _get_agent_memories

    db = SessionLocal()
    try:
        mems_a = _get_agent_memories(
            db, _NAME, "memory", org_id=_ORG_A,
        )
        mems_b = _get_agent_memories(
            db, _NAME, "memory", org_id=_ORG_B,
        )
        contents_a = {m["content"] for m in mems_a}
        contents_b = {m["content"] for m in mems_b}
        assert "MEMORY-OF-ORG-A" in contents_a
        assert "MEMORY-OF-ORG-B" not in contents_a
        assert "MEMORY-OF-ORG-B" in contents_b
        assert "MEMORY-OF-ORG-A" not in contents_b
        # The legacy bare-name row belongs to default-org only.
        assert "LEGACY-BARE-NAME-MEMORY" not in contents_a
        assert "LEGACY-BARE-NAME-MEMORY" not in contents_b
        # Org-less requests fail closed — no memories at all.
        assert _get_agent_memories(db, _NAME, "anything", org_id=None) == []
    finally:
        db.close()


def test_default_org_legacy_bridge_reaches_bare_name_rows(two_org_agents):
    """D-2 bridge: default-org callers still read pre-tenancy bare-name
    memories — but never another tenant's rows."""
    from app.services.synexia.context_assembler import _get_agent_memories

    db = SessionLocal()
    try:
        mems = _get_agent_memories(
            db, _NAME, "memory", org_id="default-org",
        )
        contents = {m["content"] for m in mems}
        assert "LEGACY-BARE-NAME-MEMORY" in contents
        assert "MEMORY-OF-ORG-A" not in contents
        assert "MEMORY-OF-ORG-B" not in contents
    finally:
        db.close()


# ── AT-3: agent + tool configuration isolation ────────────────────────────


def test_agent_and_tool_config_cannot_cross_orgs(two_org_agents):
    from app.services.synexia.context_assembler import (
        _get_agent_config,
        _get_agent_tool_config,
    )

    db = SessionLocal()
    try:
        cfg_a = _get_agent_config(db, _NAME, org_id=_ORG_A)
        cfg_b = _get_agent_config(db, _NAME, org_id=_ORG_B)
        assert cfg_a and cfg_b
        assert cfg_a["description"] == f"agent-of-{_ORG_A}"
        assert cfg_b["description"] == f"agent-of-{_ORG_B}"

        tc_a = _get_agent_tool_config(db, _NAME, org_id=_ORG_A)
        tc_b = _get_agent_tool_config(db, _NAME, org_id=_ORG_B)
        assert tc_a == {"allow": ["tool-a"]}
        assert tc_b == {"allow": ["tool-b"]}

        # Unknown agent for this org → no configuration (fail closed).
        assert _get_agent_tool_config(db, _NAME, org_id="org-unknown") is None
        assert _get_agent_config(db, _NAME, org_id="org-unknown") is None
    finally:
        db.close()


# ── AT-4: evaluation-profile isolation ─────────────────────────────────────


def test_evaluation_profile_cannot_cross_orgs(two_org_agents):
    from app.services.synexia.fsm import SynexiaFSM, ExecutionRequest

    db = SessionLocal()
    try:
        fsm = SynexiaFSM(db)

        prof_a = fsm._load_evaluation_profile(ExecutionRequest(
            agent_name=_NAME, user_message="x", org_id=_ORG_A,
            app_id="default-app",
        ))
        prof_b = fsm._load_evaluation_profile(ExecutionRequest(
            agent_name=_NAME, user_message="x", org_id=_ORG_B,
            app_id="default-app",
        ))
        assert prof_a == {"grounding_checks": [{"type": "row_count"}]}
        assert prof_b == {"grounding_checks": [{"type": "sql_only"}]}

        # The old unscoped app_id-as-PK path is GONE: request.app_id is
        # INERT — a forged foreign agent PK can no longer select ANY row.
        # Resolution falls to the org-scoped name path (org A's own row).
        prof_forged = fsm._load_evaluation_profile(ExecutionRequest(
            agent_name=_NAME, user_message="x", org_id=_ORG_A,
            app_id=two_org_agents["agent_b"],
        ))
        assert prof_forged == {"grounding_checks": [{"type": "row_count"}]}, (
            "a forged app_id changed which evaluation profile was loaded"
        )

        # Forged agent_id (foreign PK) with this org's context → None.
        prof_forged_pk = fsm._load_evaluation_profile(ExecutionRequest(
            agent_name=_NAME, agent_id=two_org_agents["agent_b"],
            user_message="x", org_id=_ORG_A, app_id="default-app",
        ))
        assert prof_forged_pk is None

        # Unknown agent → None (fail closed), never another org's profile.
        prof_none = fsm._load_evaluation_profile(ExecutionRequest(
            agent_name=_NAME, user_message="x", org_id="org-unknown",
            app_id="default-app",
        ))
        assert prof_none is None
    finally:
        db.close()


# ── AT-7: system-agent ownership (Rule-0 parity) ──────────────────────────


def test_system_agent_rows_resolve_own_org_only(two_org_agents):
    db = SessionLocal()
    try:
        sys_a = resolve_agent_in_org(
            db, agent_name="agent_builder", org_id=_ORG_A,
        )
        sys_b = resolve_agent_in_org(
            db, agent_name="agent_builder", org_id=_ORG_B,
        )
        assert sys_a is not None and sys_a.id == two_org_agents["sys_a"]
        assert sys_b is not None and sys_b.id == two_org_agents["sys_b"]
    finally:
        db.close()


# ── AT-5: executions API — authenticated-org identity + 404 walls ─────────


def _mk_user(user_id: str, org_id: str):
    from app.models.user import User

    return User(
        id=user_id,
        email=f"{user_id}@fsm-iso.test",
        full_name="FSM Iso",
        password_hash="x",
        role="admin",
        org_id=org_id,
        app_id="default-app",
        is_deleted=False,
    )


def test_create_execution_derives_org_from_authenticated_user(
    two_org_agents, monkeypatch,
):
    """The request body has no org field — identity comes from the user."""
    from app.config import settings
    from app.models.execution import Execution
    from app.routers.executions import CreateExecutionRequest, create_execution
    from app.services.synexia import fsm as fsm_module

    captured = {}

    def _fake_run(self, request):
        captured["request"] = request
        return SimpleNamespace(
            execution_id="exec-stub", state="done",
            assistant_content="", tool_calls=[], artifact_ids=[],
            confidence=1.0, confidence_factors={}, plan_summary="",
            report_card_payload={}, file_exports=[], export_artifact_id=None,
        )

    monkeypatch.setattr(fsm_module.SynexiaFSM, "run", _fake_run)
    monkeypatch.setattr(settings, "SYNEXIA_FSM_ENABLED", True)

    db = SessionLocal()
    user_a = _mk_user(f"user-fsm-a-{uuid.uuid4().hex[:6]}", _ORG_A)
    db.add(user_a)
    db.commit()
    try:
        create_execution(
            CreateExecutionRequest(
                agent_name=_NAME, user_message="hello",
            ),
            db=db, user=user_a,
        )
        req = captured["request"]
        assert req.org_id == _ORG_A, (
            "execution did not derive org from the authenticated user"
        )
        assert req.agent_id == two_org_agents["agent_a"], (
            "the in-org agent was not resolved into agent_id"
        )

        # The execution record itself is stamped with the caller's org.
        exec_row = db.query(Execution).filter(
            Execution.id == "exec-stub"
        ).first()  # stub never persisted — assert via request only
        assert exec_row is None  # stub run; persistence covered by FSM suite
    finally:
        db.close()


def test_foreign_execution_is_404_on_every_control_endpoint(two_org_agents):
    """Read/approve/reject/observations/replay of ANOTHER org's execution
    are indistinguishable from nonexistent ones (404) — BEFORE any plan
    access, clone, or node execution."""
    from datetime import datetime, timezone

    from fastapi import HTTPException

    from app.models.execution import Execution
    from app.routers.executions import (
        approve_plan,
        get_execution,
        get_observations,
        get_plan,
        reject_plan,
        replay_execution,
    )

    db = SessionLocal()
    user_a = _mk_user(f"user-fsm-xa-{uuid.uuid4().hex[:6]}", _ORG_A)
    foreign_exec = Execution(
        id=f"exec-fsm-b-{uuid.uuid4().hex[:8]}",
        agent_name=_NAME,
        user_message="org B private plan",
        current_state="plan",
        mode="dynamic",
        started_at=datetime.now(timezone.utc),
        org_id=_ORG_B,
    )
    db.add_all([user_a, foreign_exec])
    db.commit()
    try:
        for call in (
            lambda: get_execution(foreign_exec.id, db=db, user=user_a),
            lambda: get_plan(foreign_exec.id, db=db, user=user_a),
            lambda: approve_plan(foreign_exec.id, db=db, user=user_a),
            lambda: reject_plan(foreign_exec.id, db=db, user=user_a),
            lambda: get_observations(foreign_exec.id, db=db, user=user_a),
            lambda: replay_execution(
                foreign_exec.id, None, run=False, db=db, user=user_a,
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 404, (
                "a foreign-org execution was reachable from org A"
            )
    finally:
        db.close()


def test_replay_clone_is_stamped_with_callers_org(two_org_agents):
    from datetime import datetime, timezone

    from app.models.execution import Execution, Plan, PlanNode
    from app.routers.executions import replay_execution

    db = SessionLocal()
    user_a = _mk_user(f"user-fsm-ra-{uuid.uuid4().hex[:6]}", _ORG_A)
    source = Execution(
        id=f"exec-fsm-src-{uuid.uuid4().hex[:8]}",
        agent_name=_NAME,
        user_message="org A plan",
        current_state="plan",
        mode="dynamic",
        started_at=datetime.now(timezone.utc),
        org_id=_ORG_A,
    )
    db.add(source)
    db.flush()
    plan = Plan(
        execution_id=source.id, version=1, status="approved",
        summary="src", is_acyclic=True,
    )
    db.add(plan)
    db.flush()
    db.add(PlanNode(
        plan_id=plan.id, seq=1, node_type="tool", name="t",
        dependencies=[], inputs={}, expected_output="",
        output_artifact_type=None, risk_tier="low",
        requires_confirmation=False,
    ))
    db.commit()
    try:
        result = replay_execution(
            source.id, None, run=False, db=db, user=user_a,
        )
        clone = db.query(Execution).filter(
            Execution.id == result["execution_id"]
        ).first()
        assert clone is not None
        assert clone.org_id == _ORG_A, (
            "replay clone collapsed to a foreign/default org"
        )
    finally:
        db.close()


# ── FSM-tenant-closure checks A/B/C (reviewer, 2026-10-31) ────────────────


def test_check_a_forged_user_id_cannot_override_authenticated_user(
    two_org_agents, monkeypatch,
):
    """Check A: a forged ``user_id`` in the request body is inert — the
    authenticated user remains authoritative for execution identity."""
    import asyncio

    from app.config import settings
    from app.routers.executions import CreateExecutionRequest, create_execution
    from app.services.synexia import fsm as fsm_module

    captured = {}

    def _fake_run(self, request):
        captured["request"] = request
        return SimpleNamespace(
            execution_id="exec-stub-a", state="done",
            assistant_content="", tool_calls=[], artifact_ids=[],
            confidence=1.0, confidence_factors={}, plan_summary="",
            report_card_payload={}, file_exports=[], export_artifact_id=None,
        )

    monkeypatch.setattr(fsm_module.SynexiaFSM, "run", _fake_run)
    monkeypatch.setattr(settings, "SYNEXIA_FSM_ENABLED", True)

    db = SessionLocal()
    real_user = _mk_user(f"user-fsm-fa-{uuid.uuid4().hex[:6]}", _ORG_A)
    db.add(real_user)
    db.commit()
    try:
        create_execution(
            CreateExecutionRequest(
                agent_name=_NAME,
                user_message="hello",
                user_id="forged-user-0000",  # attacker-supplied identity
            ),
            db=db, user=real_user,
        )
        assert captured["request"].user_id == real_user.id, (
            "a forged request user_id overrode the authenticated user"
        )
        assert captured["request"].user_id != "forged-user-0000"
        assert captured["request"].org_id == _ORG_A
    finally:
        db.close()


def test_check_b_react_memory_write_is_org_stamped(two_org_agents, monkeypatch):
    """Check B: a ReAct (non-FSM) memory write with no explicit org in
    the per-call context must still derive the authenticated org — via
    the runtime TOOL_CONTEXT the chat loop populates — and must never
    land as an unscoped/cross-tenant row."""
    import asyncio

    from app.services import agent_tools
    from app.services.tool_handlers.memory_tool import _memory_tool

    db = SessionLocal()
    try:
        # The chat loop merges its context (which WILL carry org_id after
        # the closure fix) into TOOL_CONTEXT before every execute_tool.
        monkeypatch.setattr(
            agent_tools, "TOOL_CONTEXT", {"org_id": _ORG_A},
        )
        # Per-call context WITHOUT org — exactly the pre-fix chat shape.
        result = asyncio.get_event_loop().run_until_complete(_memory_tool(
            {"action": "add", "target": "memory",
             "content": "REACT-WRITE-ORG-A"},
            db=db, user_id=None,
            context={"agent_app_id": two_org_agents["agent_a"]},
        ))
        assert result.get("success"), f"memory write failed: {result}"

        row = db.query(AgentMemory).filter(
            AgentMemory.agent_app_id == two_org_agents["agent_a"],
            AgentMemory.content.like("%REACT-WRITE-ORG-A%"),
        ).first()
        assert row is not None, "the memory row was not written"
        assert row.org_id == _ORG_A, (
            f"a ReAct memory write landed with org_id={row.org_id!r} "
            "instead of the authenticated organization"
        )

        # And an org-B reader with the same key cannot see it.
        from app.services.memory_advanced import search_memories
        leaked = search_memories(
            db, two_org_agents["agent_a"], "REACT-WRITE",
            limit=5, min_score=0.01, org_id=_ORG_B,
        )
        assert all(
            "REACT-WRITE-ORG-A" not in r.memory.content for r in leaked
        ), "an org-A ReAct memory leaked into org B"
    finally:
        db.query(AgentMemory).filter(
            AgentMemory.content.like("%REACT-WRITE-ORG-A%")
        ).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_check_c_orgless_context_resolution_returns_no_tenant_config(
    two_org_agents,
):
    """Check C: context assembly WITHOUT an org must not perform an
    unrestricted name lookup — no agent configuration and no tool
    surface may be returned for another tenant's agent name."""
    from app.services.synexia.context_assembler import (
        _get_agent_config,
        _get_agent_tool_config,
    )

    db = SessionLocal()
    try:
        # The rows exist under _ORG_A/_ORG_B — an org-less caller must
        # get neither, even though the name matches.
        assert _get_agent_config(db, _NAME, org_id=None) is None, (
            "an org-less lookup returned tenant-specific agent configuration"
        )
        assert _get_agent_tool_config(db, _NAME, org_id=None) is None, (
            "an org-less lookup returned a tenant-specific tool surface"
        )
    finally:
        db.close()


def test_profile_lookups_share_single_identity_contract(two_org_agents):
    """Additional reviewer check: evaluation-profile resolution is NOT
    independently reimplemented — it routes through the same
    synexia.identity.resolve_agent_in_org used by every other lookup,
    and both chat paths thread the org-resolved agent_id into it."""
    import inspect

    from app.services.synexia import fsm as fsm_module

    src = inspect.getsource(fsm_module.SynexiaFSM._load_evaluation_profile)
    assert "resolve_agent_in_org" in src, (
        "evaluation-profile lookup bypasses the shared identity contract"
    )
    assert "AgentApp.id == request.app_id" not in src, (
        "the removed app_id-as-PK lookup path is back"
    )
    # The single shared resolver is THE agent-lookup used everywhere.
    import app.services.synexia.context_assembler as ca_src
    ca = inspect.getsource(ca_src)
    assert ca.count("resolve_agent_in_org") >= 2, (
        "context assembler stopped routing through the shared resolver"
    )

