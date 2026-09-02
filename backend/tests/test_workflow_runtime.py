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
