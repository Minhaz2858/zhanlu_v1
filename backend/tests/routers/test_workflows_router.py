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
from app.models.workflow_run import WorkflowRun  # noqa: E402
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
