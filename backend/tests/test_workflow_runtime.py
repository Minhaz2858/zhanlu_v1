from sqlalchemy import create_engine

from app.database import Base, SessionLocal
from app.models.agent_app import AgentApp
from app.models.workflow_run import WorkflowRun, WorkflowRunStep
from app.services import workflow_runtime as wfr

engine = create_engine("sqlite:///file::memory:?cache=shared&uri=true")
Base.metadata.create_all(bind=engine)


def _flow():
    return {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat", "config": {}}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "LLM", "config": {"prompt": "Reply to: {{inputs.n1.text}}"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }


def _agent(db, flow, aid="wf-agent-1"):
    agent = AgentApp(
        id=aid, name="test flow", flow_mode=True,
        org_id="default-org", app_id="default-app",
        flow=flow,
    )
    db.add(agent)
    db.commit()
    return agent


def test_run_workflow_agent_completes_and_persists(monkeypatch):
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "llm-answer")
    db = SessionLocal()
    agent = _agent(db, _flow())
    events = []
    result = wfr.run_workflow_agent(
        db, "default-org", "default-app", agent, "hello", on_event=events.append
    )
    assert result["status"] == "completed"
    assert result["output"] == "llm-answer"

    run = db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).first()
    assert run is not None and run.status == "completed"

    steps = db.query(WorkflowRunStep).filter(WorkflowRunStep.run_id == run.id).all()
    step_statuses = {s.node_id: s.status for s in steps}
    assert step_statuses["n1"] == "completed"
    assert step_statuses["n2"] == "completed"
    assert step_statuses["n3"] == "completed"

    event_types = [e["type"] for e in events]
    assert "run_started" in event_types
    assert "run_finished" in event_types
    assert event_types.count("step") == 6  # running + completed per node


def test_run_workflow_agent_invalid_flow_no_run(monkeypatch):
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "x")
    db = SessionLocal()
    agent = _agent(db, {"version": 1, "nodes": [], "edges": []}, aid="wf-agent-bad")
    result = wfr.run_workflow_agent(db, "default-org", "default-app", agent, "hello")
    assert result["status"] == "failed"
    assert "trigger" in result["error"]
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count() == 0


def test_run_workflow_agent_passes_history_to_trigger(monkeypatch):
    """history param reaches the chat trigger as a rendered transcript."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "llm-answer")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-agent-hist")
    wfr.run_workflow_agent(
        db, "default-org", "default-app", agent, "hello",
        history=[
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ],
    )
    run = db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).first()
    assert run is not None and run.trigger == "chat"
    # The trigger step's output persists the transcript it exposed.
    trig_step = (
        db.query(WorkflowRunStep)
        .filter(WorkflowRunStep.run_id == run.id, WorkflowRunStep.node_id == "n1")
        .order_by(WorkflowRunStep.created_date.desc())
        .first()
    )
    out = trig_step.output_json or {}
    assert "User: earlier question" in out.get("history", "")
    assert "Assistant: earlier answer" in out.get("history", "")
    assert out.get("text") == "hello"


def test_resolve_entity_by_id_or_name():
    """Config identifiers resolve by id first, then by a unique name."""
    db = SessionLocal()
    _agent(db, _flow(), aid="wf-entity-a")
    # give this row a UNIQUE name so the name lookup is unambiguous
    row = db.query(AgentApp).filter(AgentApp.id == "wf-entity-a").first()
    row.name = "unique entity name"
    db.commit()
    # id hit
    r1 = wfr._resolve_entity(db, AgentApp, "wf-entity-a")
    assert r1 is not None and r1.id == "wf-entity-a"
    # name hit (unique)
    r2 = wfr._resolve_entity(db, AgentApp, "unique entity name")
    assert r2 is not None and r2.id == "wf-entity-a"
    # unknown -> None (not a crash)
    assert wfr._resolve_entity(db, AgentApp, "does-not-exist") is None
    # empty -> None
    assert wfr._resolve_entity(db, AgentApp, "") is None
    # ambiguous name -> raises (never silently picks the wrong row)
    try:
        wfr._resolve_entity(db, AgentApp, "test flow")
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def _kb(db, kid="kb-mysql-1", db_type="mysql"):
    from app.models.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(
        id=kid, name=kid, type="database", db_type=db_type,
        host="10.0.0.1", port=3306, database_name="erp",
        username="root", password="pw",
    )
    db.add(kb)
    db.commit()
    return kb


def test_db_query_nl_uses_governed_nl2sql(monkeypatch):
    """NL-mode db_query routes through the governed nl2sql pipeline and
    maps its result to the db_query shape (dict rows + text)."""
    from app.services.nl2sql import NL2SQLResult

    db = SessionLocal()
    _kb(db)
    agent = _agent(db, _flow(), aid="wf-agent-nl2sql")

    class FakeData:
        columns = ["total_amount"]
        rows = [(18410129637.66,)]
        row_count = 1

    captured = {}

    def fake_ask(question, *, binding_id, db, datasource_config=None, **kw):
        captured["question"] = question
        captured["config"] = datasource_config
        captured["binding_id"] = binding_id
        return NL2SQLResult(success=True, question=question, sql="SELECT SUM(x)", data=FakeData())

    monkeypatch.setattr("app.services.nl2sql.ask", fake_ask)
    ctx = wfr.WorkflowExecutionContext(db, "default-org", "default-app", agent, "hello", created_by_id="u1")
    out = ctx.db_query("kb-mysql-1", "what is total sales?", mode="nl", limit=10)

    assert captured["question"] == "what is total sales?"
    assert captured["config"]["dialect"] == "mysql"
    assert captured["config"]["kb"] is not None
    # synthetic binding_id must fit audit_events.binding_id varchar(36)
    assert captured["binding_id"].startswith("wf:wf-agent")
    assert len(captured["binding_id"]) <= 36
    assert out["engine"] == "nl2sql"
    assert out["sql"] == "SELECT SUM(x)"
    assert out["rows"] == [{"total_amount": 18410129637.66}]
    assert "18410129637.66" in out["text"]


def test_db_query_nl_falls_back_when_nl2sql_fails(monkeypatch):
    """When the governed pipeline fails, NL mode falls back to the legacy
    schema-summary translator instead of crashing the node."""
    db = SessionLocal()
    _kb(db, kid="kb-mysql-2")
    agent = _agent(db, _flow(), aid="wf-agent-nl2sql-fb")

    monkeypatch.setattr("app.services.nl2sql.ask", lambda *a, **kw: None)  # unexpected failure

    class FakeConn:
        def execute(self, sql, max_rows=1, timeout_s=30):
            return [{"v": 42.0}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.services.db.connector_factory.get_connector", lambda kb: FakeConn())
    monkeypatch.setattr(
        wfr.WorkflowExecutionContext, "_translate_to_sql",
        lambda self, conn, q: "SELECT 42",
    )
    ctx = wfr.WorkflowExecutionContext(db, "default-org", "default-app", agent, "hello", created_by_id="u1")
    out = ctx.db_query("kb-mysql-2", "total?", mode="nl", limit=10)

    assert "engine" not in out  # legacy path has no engine marker
    assert out["rows"] == [{"v": 42.0}]


def test_kb_datasource_config_maps_dialects():
    from app.models.knowledge_base import KnowledgeBase

    mysql = KnowledgeBase(db_type="mysql")
    assert wfr._kb_datasource_config(mysql)["dialect"] == "mysql"
    pg = KnowledgeBase(db_type="postgresql", host="h", port=5432, database_name="d", username="u", password="p")
    cfg = wfr._kb_datasource_config(pg)
    assert cfg["dialect"] == "postgres" and cfg["database"] == "d"
    assert wfr._kb_datasource_config(KnowledgeBase(db_type="oracle")) is None
    assert wfr._kb_datasource_config(KnowledgeBase(db_type="sqlite")) is None  # no path


def test_ephemeral_context_no_rows_no_events(monkeypatch):
    """Ephemeral ctx reuses ALL handler bindings but never persists a
    WorkflowRun/WorkflowRunStep row and never emits run events."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "llm-answer")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-agent-ephemeral-1")
    before_runs = db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count()

    events = []
    ctx = wfr.WorkflowExecutionContext(
        db, "default-org", "default-app", agent, "hello",
        created_by_id="u1", on_event=events.append, ephemeral=True,
    )
    # construction must not persist a run row
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count() == before_runs

    # run the full graph through the real executor + handlers — bindings work
    result = wfr.execute_flow_graph(_flow(), ctx, wfr.NODE_HANDLERS)
    assert result["status"] == "completed"
    assert result["output"] == "llm-answer"

    # record_step / finish_run are no-ops
    ctx.record_step("n2", "running", output={"partial": True})
    ctx.record_step("n2", "completed", output={"text": "llm-answer"})
    ctx.finish_run("completed", output="llm-answer")

    assert ctx.run is None
    assert len(ctx.run_id) == 36  # still a usable run identifier
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count() == before_runs
    assert db.query(WorkflowRunStep).filter(WorkflowRunStep.run_id == ctx.run_id).count() == 0
    assert events == []  # no run_started / step / run_finished


def test_ephemeral_context_run_id_collision_with_existing_run(monkeypatch):
    """An explicit run_id in ephemeral mode must NOT touch the existing run
    row (record_step/finish_run never write through to it)."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "llm-answer")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-agent-ephemeral-2")
    ctx = wfr.WorkflowExecutionContext(db, "default-org", "default-app", agent, "hello", ephemeral=True)
    existing_run_id = ctx.run_id

    wfr.WorkflowExecutionContext(
        db, "default-org", "default-app", agent, "hello",
        run_id=existing_run_id, ephemeral=True,
    ).record_step("n9", "completed", output={"x": 1})

    assert db.query(WorkflowRun).filter(WorkflowRun.id == existing_run_id).count() == 0
    assert db.query(WorkflowRunStep).filter(WorkflowRunStep.run_id == existing_run_id).count() == 0


# ---- test_node: single-node orchestrator (iterate-loop Task 4) -------------


def test_test_node_executes_handler_with_upstream_payload(monkeypatch):
    """The llm handler runs with caller-supplied upstreams — the template
    renderer sees them via ctx.outputs and the completion prompt carries the
    payload; the handler's real (monkeypatched-completion) binding runs."""
    captured = {}

    def fake_completion(prompt, model=None):
        captured["prompt"] = prompt
        return "llm-answer"

    monkeypatch.setattr(wfr, "_sync_text_completion", fake_completion)
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-testnode-1")
    result = wfr.test_node(
        db, "default-org", "default-app", agent, "n2",
        upstreams={"n1": {"text": "hi"}},
    )
    assert result["status"] == "completed"
    assert result["node_id"] == "n2"
    assert result["node_type"] == "llm"
    assert result["error"] is None
    assert result["output"]["text"] == "llm-answer"
    assert "hi" in captured["prompt"]


def test_test_node_no_run_rows_created(monkeypatch):
    """Single-node execution is ephemeral — no WorkflowRun/WorkflowRunStep
    rows are created (same guarantee as the ephemeral ctx)."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "llm-answer")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-testnode-2")
    before_runs = db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count()
    before_steps = db.query(WorkflowRunStep).count()

    result = wfr.test_node(
        db, "default-org", "default-app", agent, "n2",
        upstreams={"n1": {"text": "hi"}},
    )
    assert result["status"] == "completed"
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count() == before_runs
    assert db.query(WorkflowRunStep).count() == before_steps


def test_test_node_unknown_node_raises(monkeypatch):
    """A node_id that is not in the workflow graph is a caller bug — raise
    ValueError instead of returning a failed result."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "x")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-testnode-3")
    try:
        wfr.test_node(db, "default-org", "default-app", agent, "does-not-exist")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_test_node_passes_retryless_single_attempt(monkeypatch):
    """A raising handler yields a failed result (no crash) and is NOT retried
    — even when the node config declares retryOnFail, test_node makes exactly
    one attempt."""
    calls = {"n": 0}

    def failing(prompt, model=None):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(wfr, "_sync_text_completion", failing)
    db = SessionLocal()
    flow = _flow()
    for n in flow["nodes"]:
        if n["id"] == "n2":
            n["data"]["config"]["retryOnFail"] = 2
    agent = _agent(db, flow, aid="wf-testnode-4")

    result = wfr.test_node(
        db, "default-org", "default-app", agent, "n2",
        upstreams={"n1": {"text": "hi"}},
    )
    assert result["status"] == "failed"
    assert result["error"] == "boom"
    assert result["output"] is None
    assert calls["n"] == 1  # retryless — exactly one attempt


def test_test_node_trigger_uses_trigger_input(monkeypatch):
    """A trigger node executed via test_node receives trigger_input as its
    chat message (same binding a persisted run would give it)."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "x")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-testnode-5")
    result = wfr.test_node(
        db, "default-org", "default-app", agent, "n1",
        trigger_input="hello",
    )
    assert result["status"] == "completed"
    out = result["output"]
    assert out["text"] == "hello"
    assert out["items"] == [{"text": "hello"}]


# ---- rerun-from-node via the SAME orchestrator (iterate-loop Task 5) -------


def test_run_workflow_agent_partial_rerun(monkeypatch):
    """Rerun-from-node flows through run_workflow_agent exactly like a full
    run: seeded upstream nodes are NOT re-executed, the rerun subgraph runs
    live through the real executor, and the new run row is tagged with
    rerun_of + rerun_from_node."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "llm-answer")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-agent-rerun-1")

    calls = {"n": 0}
    original = wfr.NODE_HANDLERS["trigger_chat"]

    def counting_trigger(ctx, node):
        calls["n"] += 1
        return original(ctx, node)

    monkeypatch.setitem(wfr.NODE_HANDLERS, "trigger_chat", counting_trigger)

    # First full run — records n1's step output for seeding.
    first = wfr.run_workflow_agent(db, "default-org", "default-app", agent, "hello")
    assert first["status"] == "completed"
    assert calls["n"] == 1
    first_run = db.query(WorkflowRun).filter(WorkflowRun.id == first["run_id"]).first()
    assert first_run is not None
    n1_step = (
        db.query(WorkflowRunStep)
        .filter(WorkflowRunStep.run_id == first["run_id"], WorkflowRunStep.node_id == "n1")
        .order_by(WorkflowRunStep.created_date.desc())
        .first()
    )
    assert n1_step is not None
    n1_output = n1_step.output_json

    # Partial rerun from n2 with n1's stored output seeded.
    second = wfr.run_workflow_agent(
        db, "default-org", "default-app", agent, "hello again",
        partial_from="n2",
        seed_outputs={"n1": n1_output},
        rerun_of=first["run_id"],
    )
    assert second["status"] == "completed"
    assert second["output"] == "llm-answer"
    assert calls["n"] == 1  # seeded trigger NOT executed again

    new_run = db.query(WorkflowRun).filter(WorkflowRun.id == second["run_id"]).first()
    assert new_run is not None
    assert new_run.rerun_of == first["run_id"]
    assert new_run.rerun_from_node == "n2"
    # Only the rerun subgraph leaves step rows under the new run.
    new_steps = db.query(WorkflowRunStep).filter(WorkflowRunStep.run_id == second["run_id"]).all()
    assert {s.node_id for s in new_steps} == {"n2", "n3"}


def test_run_workflow_agent_full_run_has_null_rerun_columns(monkeypatch):
    """A plain full run (no rerun kwargs) persists rerun_of and
    rerun_from_node as NULL — defaults keep existing callers
    byte-identical."""
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "llm-answer")
    db = SessionLocal()
    agent = _agent(db, _flow(), aid="wf-agent-rerun-null")
    result = wfr.run_workflow_agent(db, "default-org", "default-app", agent, "hello")
    assert result["status"] == "completed"
    run = db.query(WorkflowRun).filter(WorkflowRun.id == result["run_id"]).first()
    assert run is not None
    assert run.rerun_of is None
    assert run.rerun_from_node is None

