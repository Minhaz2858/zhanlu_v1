"""Comprehensive parity + integration + regression tests for the
``automation_runtime_agent``.

These pin the safe-parity contract between the hidden unattended executor
(``automation_runtime_agent``) and ``general_assistant``:

  * Toolset: the runtime agent carries every general-purpose tool
    ``general_assistant`` has, MINUS the documented admin exclusions
    (``docker_compose_restart`` / ``update_env_config`` / ``cronjob``) and
    automation/agent CRUD. ``execute_automation`` IS granted (capped).
  * Harness + policy: the runtime agent carries the same ``_BASE_HARNESS``
    (output_contract / evaluation_profile / data_bindings / skill_bindings /
    trace) and ``policy_profile`` as ``general_assistant``.
  * Prompt: the extended identity prompt names the full toolset and keeps the
    data-isolation / anti-mutation guardrails.
  * Model: inherits ``settings.LLM_MODEL`` (provider-agnostic).
  * Idempotent refresh: already-provisioned runtime agents upgrade to the
    current parity config on next resolve.
  * Recursion cap: ``execute_automation`` refuses to spawn beyond
    ``AUTOMATION_MAX_RECURSION_DEPTH``; ``parent_execution_id`` stamps the
    chain; ``compute_execution_depth`` walks it correctly.

Run (RAM-safe, explicit path only):
  cd /root/zhanlu/backend && rm -f test_runtime.db && \
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -c \
  "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && \
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_automation_runtime_parity.py -v
"""
import asyncio
import uuid

from app.database import SessionLocal
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.models.user import User
from app.services.automation_chat_tool import execute_automation_tool
from app.services.automation_runtime import (
    RUNTIME_ENABLED_TOOLS,
    _runtime_model,
    ensure_automation_runtime_agent,
)
from app.services.automation_dispatcher import (
    AUTOMATION_MAX_RECURSION_DEPTH,
    compute_execution_depth,
    trigger_now,
)
from app.services.system_agents import _build_system_agent_configs


# Tools general_assistant has that the runtime agent MUST NOT gain (admin).
# (general_assistant does not carry cronjob; automation/agent CRUD are not in
# general_assistant's list either — they stay forbidden on the runtime side.)
_ADMIN_EXCLUSIONS = {"docker_compose_restart", "update_env_config"}

# Tools the runtime agent must NEVER carry, regardless of parity scope.
_FORBIDDEN = {
    "create_automation", "update_automation", "delete_automation",
    "create_agent", "update_agent", "delete_agent",
    "docker_compose_restart", "update_env_config", "cronjob",
}

_UID = uuid.uuid4().hex[:8]


def _e(suffix):
    return f"{suffix}-{_UID}@x.com"


def _ga_config():
    """Return general_assistant's config dict (registry=None → full tool list)."""
    return {c["name"]: c for c in _build_system_agent_configs(None)}["general_assistant"]


# ---------------------------------------------------------------------------
# 1. Toolset parity
# ---------------------------------------------------------------------------
def test_runtime_toolset_is_safe_parity_superset_of_general_assistant():
    """Every general_assistant general-purpose tool (minus admin exclusions)
    must be present on the runtime agent."""
    db = SessionLocal()
    try:
        org, app = f"parity-org-{_UID}", "parity-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        rt_tools = set((runtime.tool_config or {}).get("enabled_tools", []))
        ga_tools = set(_ga_config()["tool_config"]["enabled_tools"])

        missing = (ga_tools - _ADMIN_EXCLUSIONS) - rt_tools
        assert not missing, (
            f"runtime agent is missing general_assistant tools (after admin "
            f"exclusions): {sorted(missing)}"
        )
        # Spot-check the headline general-purpose capabilities are present.
        for tool in [
            "execute_code", "image_generation", "agent_browser", "skills",
            "execute_automation", "clarify", "delegate_task", "web_extract",
            "kanban", "run_sandbox_skill", "load_skill_body", "osv_check",
        ]:
            assert tool in rt_tools, f"runtime agent missing parity tool {tool!r}"
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_runtime_toolset_excludes_admin_and_crud():
    """Admin + automation/agent CRUD must NEVER be on the runtime agent."""
    db = SessionLocal()
    try:
        org, app = f"excl-org-{_UID}", "excl-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        rt_tools = set((runtime.tool_config or {}).get("enabled_tools", []))
        leak = rt_tools & _FORBIDDEN
        assert not leak, f"runtime agent gained forbidden tools: {sorted(leak)}"
        # disabled_tools must list every forbidden tool.
        disabled = set((runtime.tool_config or {}).get("disabled_tools", []))
        assert _FORBIDDEN <= disabled
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. Harness + policy parity
# ---------------------------------------------------------------------------
def test_runtime_carries_base_harness_matching_general_assistant():
    """The runtime agent must carry the same _BASE_HARNESS fields as
    general_assistant (output_contract, evaluation_profile, data_bindings,
    skill_bindings, trace_enabled, log_level, memory_scope)."""
    db = SessionLocal()
    try:
        org, app = f"harn-org-{_UID}", "harn-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        ga = _ga_config()
        assert runtime.output_contract == ga["output_contract"]
        assert runtime.evaluation_profile == ga["evaluation_profile"]
        assert runtime.data_bindings == ga["data_bindings"]
        assert runtime.skill_bindings == ga["skill_bindings"]
        assert runtime.trace_enabled == ga["trace_enabled"]
        assert runtime.log_level == ga["log_level"]
        assert runtime.memory_scope == ga["memory_scope"]
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_runtime_carries_policy_profile_matching_general_assistant():
    """The runtime agent must carry a policy_profile matching general_assistant
    (low risk, no confirmation gate so unattended runs don't stall)."""
    db = SessionLocal()
    try:
        org, app = f"pol-org-{_UID}", "pol-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        ga = _ga_config()
        assert runtime.policy_profile == ga["policy_profile"]
        # Unattended runs must not block on confirmation.
        assert runtime.policy_profile["requires_confirmation"] is False
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. Prompt parity
# ---------------------------------------------------------------------------
def test_runtime_prompt_covers_full_toolset_and_guardrails():
    """The extended identity prompt must name the full toolset AND keep the
    data-isolation + anti-mutation guardrails."""
    db = SessionLocal()
    try:
        org, app = f"prompt-org-{_UID}", "prompt-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        prompt = runtime.prompt_identity or ""
        # Full toolset advertised.
        for tool in [
            "execute_code", "image_generation", "agent_browser", "skills",
            "execute_automation", "clarify", "delegate_task", "web_extract",
            "memory", "create_artifact", "send_message",
        ]:
            assert tool in prompt, f"prompt does not mention tool {tool!r}"
        # Data-isolation guardrail.
        assert "project_id" in prompt
        # Anti-mutation guardrail (never create/update/delete automations or
        # agents; never use admin tools).
        assert "NEVER" in prompt
        assert "docker_compose_restart" in prompt  # explicitly called out
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. Model parity
# ---------------------------------------------------------------------------
def test_runtime_model_inherits_configured_llm_model():
    """The runtime agent must inherit settings.LLM_MODEL (provider-agnostic),
    not hardcode general_assistant's 'gpt-4o' (which 400s on non-OpenAI
    endpoints)."""
    db = SessionLocal()
    try:
        org, app = f"model-org-{_UID}", "model-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        assert runtime.model == _runtime_model()
        assert runtime.model is not None
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. Idempotent refresh
# ---------------------------------------------------------------------------
def test_refresh_upgrades_old_13tool_row_to_parity_config():
    """An already-provisioned runtime agent with the OLD narrow config must be
    upgraded to the parity toolset+harness+prompt on the next resolve."""
    db = SessionLocal()
    try:
        org, app = f"ref-org-{_UID}", "ref-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        # Simulate a pre-parity row: narrow tool list, no harness, old prompt.
        runtime.tool_config = {"enabled_tools": ["web_search", "memory"], "disabled_tools": []}
        runtime.prompt_identity = "OLD NARROW PROMPT"
        runtime.output_contract = None
        runtime.policy_profile = None
        db.commit()

        refreshed = ensure_automation_runtime_agent(db, org, app)
        assert refreshed.id == runtime.id
        enabled = set((refreshed.tool_config or {}).get("enabled_tools", []))
        assert "execute_code" in enabled
        assert "execute_automation" in enabled
        assert "image_generation" in enabled
        assert refreshed.output_contract is not None
        assert refreshed.policy_profile is not None
        assert "FULL" in (refreshed.prompt_identity or "")
        db.delete(refreshed)
        db.commit()
    finally:
        db.close()


def test_idempotent_refresh_is_noop_when_already_current():
    """Repeated resolves after steady-state must not error and must keep the
    parity config (the refresh path is idempotent)."""
    db = SessionLocal()
    try:
        org, app = f"idem-org-{_UID}", "idem-app"
        a = ensure_automation_runtime_agent(db, org, app)
        b = ensure_automation_runtime_agent(db, org, app)
        c = ensure_automation_runtime_agent(db, org, app)
        assert a.id == b.id == c.id
        enabled = set((c.tool_config or {}).get("enabled_tools", []))
        assert "execute_code" in enabled
        assert c.output_contract is not None
        db.delete(c)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. Recursion guard — compute_execution_depth
# ---------------------------------------------------------------------------
def _make_task(db, owner_id, name="recursion task"):
    t = AutomationTask(
        id=f"tsk-{uuid.uuid4().hex[:12]}",
        name=name,
        type="custom",
        prompt="summarise",
        schedule="manual",
        status="active",
        created_by_id=owner_id,
        org_id="default-org",
        app_id="default-app",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_chain(db, task, depth):
    """Create a parent_execution_id chain of length ``depth``: E1→E2→…→E{depth}."""
    ids = [f"chain-{_UID}-{i}" for i in range(1, depth + 1)]
    rows = []
    for i, eid in enumerate(ids):
        parent = ids[i - 1] if i > 0 else None
        rows.append(AutomationExecution(
            id=eid,
            automation_task_id=task.id,
            status="completed",
            org_id="default-org",
            app_id="default-app",
            parent_execution_id=parent,
        ))
    db.add_all(rows)
    db.commit()
    return ids


def _cleanup_task(task_name, email):
    """Delete executions + task + user created by a recursion test, using a
    fresh session (the test's own session may already be closed)."""
    with SessionLocal() as cdb:
        task_ids = [t.id for t in cdb.query(AutomationTask).filter(
            AutomationTask.name == task_name).all()]
        if task_ids:
            for r in cdb.query(AutomationExecution).filter(
                AutomationExecution.automation_task_id.in_(task_ids)
            ).all():
                cdb.delete(r)
        for t in cdb.query(AutomationTask).filter(
            AutomationTask.name == task_name).all():
            cdb.delete(t)
        u = cdb.query(User).filter(User.email == email).first()
        if u:
            cdb.delete(u)
        cdb.commit()


def test_compute_execution_depth_none_returns_zero():
    db = SessionLocal()
    try:
        assert compute_execution_depth(db, None) == 0
        assert compute_execution_depth(db, "") == 0
    finally:
        db.close()


def test_compute_execution_depth_walks_parent_chain():
    db = SessionLocal()
    try:
        owner = User(id=f"u-depth-{_UID}", email=_e("depth"), full_name="Depth",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, owner.id)
        ids = _make_chain(db, task, 4)  # E1..E4
        assert compute_execution_depth(db, ids[0]) == 1  # E1 top-level
        assert compute_execution_depth(db, ids[1]) == 2
        assert compute_execution_depth(db, ids[2]) == 3
        assert compute_execution_depth(db, ids[3]) == 4
        # Missing id → 0 (can't prove nesting → treated as top-level-ish).
        assert compute_execution_depth(db, "no-such-id") == 0
        # cleanup
        for eid in reversed(ids):
            ex = db.query(AutomationExecution).filter(AutomationExecution.id == eid).first()
            if ex:
                db.delete(ex)
        db.delete(task)
        db.delete(owner)
        db.commit()
    finally:
        db.close()


def test_compute_execution_depth_handles_cycle_without_hanging():
    """A cyclic parent_execution_id chain must not loop forever — the seen-set
    + hard guard cap bound the walk."""
    db = SessionLocal()
    try:
        owner = User(id=f"u-cyc-{_UID}", email=_e("cyc"), full_name="Cyc",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, owner.id, name="cycle task")
        c1, c2 = f"cyc-{_UID}-1", f"cyc-{_UID}-2"
        db.add_all([
            AutomationExecution(id=c1, automation_task_id=task.id, status="completed",
                                org_id="default-org", app_id="default-app",
                                parent_execution_id=c2),
            AutomationExecution(id=c2, automation_task_id=task.id, status="completed",
                                org_id="default-org", app_id="default-app",
                                parent_execution_id=c1),
        ])
        db.commit()
        depth = compute_execution_depth(db, c1)
        assert depth >= 1
        assert depth <= AUTOMATION_MAX_RECURSION_DEPTH + 3  # guard-bounded
    finally:
        db.close()
        _cleanup_task("cycle task", _e("cyc"))


def test_get_current_execution_id_defaults_to_none_outside_run():
    """Outside an automation run, get_current_execution_id() is None — so
    execute_automation called from interactive chat is treated as top-level
    (parent_execution_id=NULL). Guards against the contextvar ever leaking."""
    from app.services.automation_executor import get_current_execution_id
    assert get_current_execution_id() is None


# ---------------------------------------------------------------------------
# 7. Recursion guard — trigger_now stamps parent_execution_id
# ---------------------------------------------------------------------------
def test_trigger_now_stamps_parent_execution_id(monkeypatch):
    """trigger_now must persist parent_execution_id on the spawned execution."""
    async def _noop_run_executor(execution_id):  # noqa: ARG001
        return None
    monkeypatch.setattr(
        "app.services.automation_dispatcher._run_executor", _noop_run_executor,
    )
    db = SessionLocal()
    created_exec_id = None
    try:
        owner = User(id=f"u-tn-{_UID}", email=_e("tn"), full_name="TN",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, owner.id, name="parent stamp task")

        async def _do():
            eid = await trigger_now(task.id, parent_execution_id="parent-exec-xyz")
            await asyncio.sleep(0)  # let the no-op _run_executor task finish
            return eid
        created_exec_id = _run(_do())
        assert created_exec_id

        # Read it back from a fresh session.
        with SessionLocal() as rdb:
            row = rdb.query(AutomationExecution).filter(
                AutomationExecution.id == created_exec_id
            ).first()
            assert row is not None
            assert row.parent_execution_id == "parent-exec-xyz"
    finally:
        if created_exec_id:
            with SessionLocal() as cdb:
                ex = cdb.query(AutomationExecution).filter(
                    AutomationExecution.id == created_exec_id
                ).first()
                if ex:
                    cdb.delete(ex)
                    cdb.commit()
        # cleanup task + owner
        with SessionLocal() as cdb:
            t = cdb.query(AutomationTask).filter(
                AutomationTask.name == "parent stamp task"
            ).first()
            if t:
                cdb.delete(t)
            u = cdb.query(User).filter(User.email == _e("tn")).first()
            if u:
                cdb.delete(u)
            cdb.commit()
        db.close()


# ---------------------------------------------------------------------------
# 8. Recursion guard — execute_automation_tool refuses beyond cap
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


def test_execute_automation_refuses_beyond_recursion_cap(monkeypatch):
    """When the current run is already at the max depth, execute_automation
    must refuse to spawn another nested run (no trigger_now call)."""
    monkeypatch.setattr(
        "app.services.automation_chat_tool._poll_execution_status",
        lambda db, eid, timeout=5.0: {"status": "completed", "output_text": "ok"},  # noqa: ARG005
    )
    spawn_calls = []

    async def _spying_trigger_now(task_id, parent_execution_id=None):  # noqa: ARG001
        spawn_calls.append(parent_execution_id)
        return "should-not-happen"

    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now", _spying_trigger_now,
    )
    from app.services.automation_executor import _CURRENT_EXECUTION_ID

    db = SessionLocal()
    try:
        owner = User(id=f"u-ref-{_UID}", email=_e("ref"), full_name="Ref",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, owner.id, name="refuse task")
        # Build a chain at exactly the max depth. The current run = the
        # deepest node; a child would exceed the cap.
        ids = _make_chain(db, task, AUTOMATION_MAX_RECURSION_DEPTH)
        current_exec_id = ids[-1]

        token = _CURRENT_EXECUTION_ID.set(current_exec_id)
        try:
            result = _run(execute_automation_tool(
                {"task_id": task.id}, db, owner.id,
            ))
        finally:
            _CURRENT_EXECUTION_ID.reset(token)

        assert result["success"] is False
        assert "recursion" in result["error"].lower()
        assert spawn_calls == [], "trigger_now must NOT be called when over cap"
    finally:
        db.close()
        _cleanup_task("refuse task", _e("ref"))


def test_execute_automation_allows_within_recursion_cap(monkeypatch):
    """When the current run is below the max depth, execute_automation spawns
    the nested run (stamping the current execution as parent)."""
    monkeypatch.setattr(
        "app.services.automation_chat_tool._poll_execution_status",
        lambda db, eid, timeout=5.0: {"status": "completed", "output_text": "ok"},  # noqa: ARG005
    )
    captured_parent = {}

    async def _capturing_trigger_now(task_id, parent_execution_id=None):  # noqa: ARG001
        captured_parent["parent"] = parent_execution_id
        return "child-exec-123"

    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now", _capturing_trigger_now,
    )
    from app.services.automation_executor import _CURRENT_EXECUTION_ID

    db = SessionLocal()
    try:
        owner = User(id=f"u-allow-{_UID}", email=_e("allow"), full_name="Allow",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, owner.id, name="allow task")
        # Build a chain one SHORTER than the cap so a child is still allowed.
        ids = _make_chain(db, task, max(1, AUTOMATION_MAX_RECURSION_DEPTH - 1))
        current_exec_id = ids[-1]

        token = _CURRENT_EXECUTION_ID.set(current_exec_id)
        try:
            result = _run(execute_automation_tool(
                {"task_id": task.id}, db, owner.id,
            ))
        finally:
            _CURRENT_EXECUTION_ID.reset(token)

        assert result["success"] is True
        assert result["execution_id"] == "child-exec-123"
        # The current run's id must be stamped as the parent of the child.
        assert captured_parent.get("parent") == current_exec_id
    finally:
        db.close()
        _cleanup_task("allow task", _e("allow"))


def test_execute_automation_from_chat_is_top_level(monkeypatch):
    """When called from interactive chat (no current execution id), the spawn
    is top-level (parent_execution_id=None) — no behaviour change for chat."""
    monkeypatch.setattr(
        "app.services.automation_chat_tool._poll_execution_status",
        lambda db, eid, timeout=5.0: {"status": "completed", "output_text": "ok"},  # noqa: ARG005
    )
    captured_parent = {}

    async def _capturing_trigger_now(task_id, parent_execution_id=None):  # noqa: ARG001
        captured_parent["parent"] = parent_execution_id
        return "chat-exec-123"

    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now", _capturing_trigger_now,
    )
    db = SessionLocal()
    try:
        owner = User(id=f"u-chat-{_UID}", email=_e("chat"), full_name="Chat",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, owner.id, name="chat task")
        result = _run(execute_automation_tool(
            {"task_id": task.id}, db, owner.id,
        ))
        assert result["success"] is True
        assert captured_parent.get("parent") is None  # top-level
    finally:
        db.close()
        _cleanup_task("chat task", _e("chat"))


# ---------------------------------------------------------------------------
# 9. Schema regression — parent_execution_id column exists
# ---------------------------------------------------------------------------
def test_parent_execution_id_column_exists():
    """The automation_executions table must carry parent_execution_id."""
    from sqlalchemy import inspect
    from app.database import engine
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("automation_executions")}
    assert "parent_execution_id" in cols


def test_runtime_enabled_tools_constant_has_parity_set():
    """Sanity: the RUNTIME_ENABLED_TOOLS constant carries the parity set
    (regression guard against an accidental revert to the old 13-tool list)."""
    tools = set(RUNTIME_ENABLED_TOOLS)
    for tool in [
        "execute_code", "image_generation", "agent_browser", "skills",
        "execute_automation", "clarify", "delegate_task", "web_extract",
        "kanban", "run_sandbox_skill", "create_artifact", "send_message",
        "list_knowledge_bases", "answer_from_database",
    ]:
        assert tool in tools, f"RUNTIME_ENABLED_TOOLS missing {tool!r}"
    assert not (tools & _FORBIDDEN)
