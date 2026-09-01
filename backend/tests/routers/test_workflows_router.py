"""Router tests: workflows endpoints, called directly (no TestClient).

Covers create/list/save/validate/run/publish plus the user-scope wall
(non-admin cannot read another user's workflow agent).
"""

import sys
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402  register all models
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.workflow_run import WorkflowRun, WorkflowRunStep  # noqa: E402
from app.routers import workflows as wf_mod  # noqa: E402
from app.services import workflow_runtime as wfr  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture
def db():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


def _user(uid="user-1", role="user"):
    return SimpleNamespace(id=uid, role=role)


def _flow():
    return {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat", "config": {}}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "LLM", "config": {"prompt": "hi"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }


def _mk_workflow(db, *, owner="user-1", flow=None, status="draft", flow_mode=True):
    wf = AgentApp(
        id=str(uuid.uuid4()), name="wf", flow_mode=flow_mode, flow=flow,
        status=status, app_id="app-1", org_id="default-org",
        created_by_id=owner,
    )
    db.add(wf)
    db.commit()
    return wf


def test_create_workflow_makes_empty_flow_agent(db):
    out = wf_mod.create_workflow("app-1", db=db, user=_user())
    assert out["flow_mode"] is True
    assert out["flow"] == {"version": 1, "nodes": [], "edges": []}
    assert out["status"] == "draft"


def test_list_workflows_scoped_and_flow_mode_only(db):
    wf_mod.create_workflow("app-1", db=db, user=_user("user-1"))
    wf_mod.create_workflow("app-1", db=db, user=_user("user-2"))
    _mk_workflow(db, owner="user-1", flow_mode=False)  # plain agent — excluded
    out = wf_mod.list_workflows("app-1", db=db, user=_user("user-1"))
    assert len(out) == 1
    assert out[0]["created_by_id"] == "user-1"


def test_save_workflow_invalid_flow_422(db):
    wf = _mk_workflow(db, flow=_flow())
    with pytest.raises(HTTPException) as exc:
        wf_mod.save_workflow(
            wf.id, wf_mod.SaveBody(flow={"version": 1, "nodes": [], "edges": []}),
            "app-1", db=db, user=_user(),
        )
    assert exc.value.status_code == 422
    assert any("trigger" in e for e in exc.value.detail)


def test_save_workflow_valid_saves_graph(db):
    wf = _mk_workflow(db, flow=_flow())
    new_flow = _flow()
    new_flow["nodes"][1]["data"]["config"]["prompt"] = "updated"
    out = wf_mod.save_workflow(
        wf.id, wf_mod.SaveBody(name="Renamed", flow=new_flow),
        "app-1", db=db, user=_user(),
    )
    assert out["name"] == "Renamed"
    assert out["flow"]["nodes"][1]["data"]["config"]["prompt"] == "updated"


def test_save_workflow_normalizes_missing_edge_ids(db):
    """Edges without ids (AI-builder output) get stable e1..eN ids.

    xyflow requires a unique id per edge — null ids would render as
    unconnected nodes on the canvas, so the PUT path must normalize.
    """
    wf = _mk_workflow(db, flow=_flow())
    new_flow = _flow()
    for e in new_flow["edges"]:
        e.pop("id", None)
    out = wf_mod.save_workflow(
        wf.id, wf_mod.SaveBody(flow=new_flow),
        "app-1", db=db, user=_user(),
    )
    ids = [e["id"] for e in out["flow"]["edges"]]
    assert ids == ["e1", "e2"]
    # Existing ids are preserved untouched.
    preserved = _flow()
    preserved["edges"][0]["id"] = "keep-me"
    out2 = wf_mod.save_workflow(
        wf.id, wf_mod.SaveBody(flow=preserved),
        "app-1", db=db, user=_user(),
    )
    assert [e["id"] for e in out2["flow"]["edges"]] == ["keep-me", "e2"]


def test_validate_endpoint(db):
    wf = _mk_workflow(db, flow=_flow())
    ok = wf_mod.validate_workflow(wf.id, "app-1", db=db, user=_user())
    assert ok == {"valid": True, "errors": []}
    wf.flow = {"version": 1, "nodes": [], "edges": []}
    db.commit()
    bad = wf_mod.validate_workflow(wf.id, "app-1", db=db, user=_user())
    assert bad["valid"] is False


def test_run_workflow_completes(db, monkeypatch):
    monkeypatch.setattr(wfr, "_sync_text_completion", lambda prompt, model=None: "answer")
    wf = _mk_workflow(db, flow=_flow())
    out = wf_mod.run_workflow(wf.id, wf_mod.RunBody(input="hello"), "app-1", db=db, user=_user())
    assert out["status"] == "completed"
    assert out["output"] == "answer"
    assert out["run_id"]
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == wf.id).count() == 1


def test_publish_gate(db):
    wf = _mk_workflow(db, flow={"version": 1, "nodes": [], "edges": []})
    with pytest.raises(HTTPException) as exc:
        wf_mod.publish_workflow(wf.id, "app-1", db=db, user=_user())
    assert exc.value.status_code == 422
    wf.flow = _flow()
    db.commit()
    out = wf_mod.publish_workflow(wf.id, "app-1", db=db, user=_user())
    assert out["status"] == "published"


def test_other_users_workflow_is_404(db):
    wf = _mk_workflow(db, owner="user-2", flow=_flow())
    with pytest.raises(HTTPException) as exc:
        wf_mod.get_workflow(wf.id, "app-1", db=db, user=_user("user-1"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Error workflow (n8n Error Workflow): flow.settings.on_error_workflow_id
# runs automatically when the main workflow fails — one level deep.
# ---------------------------------------------------------------------------

def _error_flow_workflow(db, owner="user-1"):
    """trigger_manual -> output: echoes the JSON failure payload it receives."""
    flow = {
        "version": 1,
        "nodes": [
            {"id": "e1", "type": "trigger_manual", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "e2", "type": "output", "position": {"x": 1, "y": 0}, "data": {"label": "O", "config": {}}},
        ],
        "edges": [{"id": "ee1", "source": "e1", "target": "e2"}],
    }
    return _mk_workflow(db, owner=owner, flow=flow)


def test_run_failure_triggers_error_workflow(db, monkeypatch):
    def boom(prompt, model=None):
        raise RuntimeError("llm provider down")

    monkeypatch.setattr(wfr, "_sync_text_completion", boom)
    err_wf = _error_flow_workflow(db)
    main_flow = _flow()
    main_flow["settings"] = {"on_error_workflow_id": err_wf.id}
    wf = _mk_workflow(db, flow=main_flow)

    out = wf_mod.run_workflow(wf.id, wf_mod.RunBody(input="hi"), "app-1", db=db, user=_user())
    assert out["status"] == "failed"
    assert "llm provider down" in out["error"]
    assert out["failed_node_id"] == "n2"
    assert out["error_workflow_run_id"]
    assert out["error_workflow_status"] == "completed"

    # The error workflow executed and its output node received the failure
    # context (JSON payload containing the error message).
    err_steps = (
        db.query(WorkflowRunStep)
        .filter(WorkflowRunStep.run_id == out["error_workflow_run_id"])
        .order_by(WorkflowRunStep.created_date.asc())
        .all()
    )
    output_text = ""
    for s in err_steps:
        if s.node_id == "e2" and s.status == "completed":
            output_text = (s.output_json or {}).get("text", "")
    assert "llm provider down" in output_text
    assert '"workflow_name": "wf"' in output_text
    assert '"node_type": "llm"' in output_text

    # Error workflow runs are recorded as their own WorkflowRun rows.
    err_run = db.query(WorkflowRun).filter(WorkflowRun.id == out["error_workflow_run_id"]).first()
    assert err_run is not None
    assert err_run.agent_app_id == err_wf.id
    assert err_run.trigger == "error"


def test_run_failure_without_error_workflow_has_no_extra(db, monkeypatch):
    def boom(prompt, model=None):
        raise RuntimeError("down")

    monkeypatch.setattr(wfr, "_sync_text_completion", boom)
    wf = _mk_workflow(db, flow=_flow())  # no settings
    out = wf_mod.run_workflow(wf.id, wf_mod.RunBody(input="hi"), "app-1", db=db, user=_user())
    assert out["status"] == "failed"
    assert "error_workflow_run_id" not in out
    # Only the main run exists — no error workflow was spawned.
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == wf.id).count() == 1


def test_error_workflow_does_not_recurse(db, monkeypatch):
    """A failing error workflow never spawns another error workflow."""
    def boom(prompt, model=None):
        raise RuntimeError("down")

    monkeypatch.setattr(wfr, "_sync_text_completion", boom)
    # Error workflow whose LLM node ALSO fails (same monkeypatched provider).
    err_flow = _flow()  # trigger_chat -> llm -> output
    err_wf = _mk_workflow(db, flow=err_flow)
    main_flow = _flow()
    main_flow["settings"] = {"on_error_workflow_id": err_wf.id}
    wf = _mk_workflow(db, flow=main_flow)

    out = wf_mod.run_workflow(wf.id, wf_mod.RunBody(input="hi"), "app-1", db=db, user=_user())
    assert out["status"] == "failed"
    assert out["error_workflow_run_id"]
    assert out["error_workflow_status"] == "failed"
    # Exactly 2 runs total: the main one + the error one. No recursion.
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id.in_([wf.id, err_wf.id])).count() == 2
