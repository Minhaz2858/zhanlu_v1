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
