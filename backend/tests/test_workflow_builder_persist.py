"""Tests: workflow_builder decision-summary persistence (Task 4).

Covers ``save_flow_from_decision_summary`` + ``resolve_entity_references``
in app.services.workflow_runtime — the backend branch that turns a
workflow_builder ``:::decision-summary`` (action == "save_flow") into a
persisted draft workflow.

Router-direct pattern (no TestClient): StaticPool SQLite session,
SimpleNamespace user — mirrors tests/routers/test_workflows_router.py.
"""

import sys
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402  register all models
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.mcp_server import McpServer  # noqa: E402
from app.models.tool import Tool  # noqa: E402
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


def _mk_workflow(db, wf_id="wf-1", owner="user-1"):
    wf = AgentApp(
        id=wf_id,
        name="Order Summary",
        description="",
        flow_mode=True,
        flow={"version": 1, "nodes": [], "edges": []},
        status="draft",
        app_id="default-app",
        org_id="default-org",
        created_by_id=owner,
    )
    db.add(wf)
    db.commit()
    return wf


def _valid_payload():
    return {
        "action": "save_flow",
        "name": "Order Summary",
        "description": "Queries open orders",
        "flow": {
            "version": 1,
            "nodes": [
                {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat Trigger", "config": {}}},
                {"id": "n2", "type": "output", "position": {"x": 200, "y": 0}, "data": {"label": "Output", "config": {}}},
            ],
            "edges": [{"id": "e1", "source": "n1", "sourceHandle": "source-main", "target": "n2", "targetHandle": "target-main"}],
        },
    }


def test_save_flow_valid_persists_draft(db, monkeypatch):
    _mk_workflow(db)
    payload = _valid_payload()
    saved = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert saved["status"] == "flow_saved"
    assert saved["flow"]["nodes"][0]["type"] == "trigger_chat"

    row = db.query(AgentApp).filter(AgentApp.id == "wf-1").first()
    assert row is not None
    assert row.name == "Order Summary"
    assert row.description == "Queries open orders"
    assert row.flow["nodes"][0]["type"] == "trigger_chat"


def test_save_flow_invalid_returns_errors(db):
    _mk_workflow(db)
    payload = {"action": "save_flow", "name": "Bad", "flow": {"version": 1, "nodes": [], "edges": []}}  # no trigger
    out = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert out["status"] == "flow_invalid"
    assert any("trigger" in e.lower() for e in out["errors"])

    # Nothing persisted on invalid.
    row = db.query(AgentApp).filter(AgentApp.id == "wf-1").first()
    assert row.flow == {"version": 1, "nodes": [], "edges": []}


def test_save_flow_missing_nodes_shape_returns_errors(db):
    _mk_workflow(db)
    payload = {"action": "save_flow", "name": "Bad", "flow": {"version": 1}}
    out = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert out["status"] == "flow_invalid"
    assert any("nodes" in e.lower() for e in out["errors"])


def _entity_ref_flow():
    return {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat", "config": {}}},
            {"id": "n2", "type": "agent", "position": {"x": 1, "y": 0}, "data": {"label": "Agent", "config": {"agent_id": "Order Agent"}}},
            {"id": "n3", "type": "tool", "position": {"x": 2, "y": 0}, "data": {"label": "Tool", "config": {"tool_id": "Web Search"}}},
            {"id": "n4", "type": "kb_search", "position": {"x": 3, "y": 0}, "data": {"label": "KB", "config": {"kb_id": "Products KB"}}},
            {"id": "n5", "type": "mcp_tool", "position": {"x": 4, "y": 0}, "data": {"label": "MCP", "config": {"server_id": "GitHub MCP", "tool_name": "list_repos"}}},
            {"id": "n6", "type": "output", "position": {"x": 5, "y": 0}, "data": {"label": "Out", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
            {"id": "e3", "source": "n3", "target": "n4"},
            {"id": "e4", "source": "n4", "target": "n5"},
            {"id": "e5", "source": "n5", "target": "n6"},
        ],
    }


def _seed_entities(db):
    db.add(AgentApp(id="agent-1", name="Order Agent", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.add(Tool(id="tool-1", name="Web Search", app_id="default-app",
                org_id="default-org", created_by_id="user-1"))
    db.add(KnowledgeBase(id="kb-1", name="Products KB", app_id="default-app",
                         org_id="default-org", created_by_id="user-1"))
    db.add(McpServer(id="srv-1", name="GitHub MCP", server_url="http://mcp.local",
                     app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.commit()


def test_resolve_entity_references_replaces_names_with_ids(db):
    _seed_entities(db)
    flow = _entity_ref_flow()
    notes = wfr.resolve_entity_references(flow, db)

    configs = {n["id"]: n["data"]["config"] for n in flow["nodes"]}
    assert configs["n2"]["agent_id"] == "agent-1"
    assert configs["n3"]["tool_id"] == "tool-1"
    assert configs["n4"]["kb_id"] == "kb-1"
    assert configs["n5"]["server_id"] == "srv-1"
    # Already-id values are left untouched (no note, no change).
    assert configs["n5"]["tool_name"] == "list_repos"
    assert any("n2" in note and "agent" in note for note in notes)


def test_resolve_entity_references_skips_uuid_ids_and_unknown(db):
    _seed_entities(db)
    flow = _entity_ref_flow()
    flow["nodes"][1]["data"]["config"]["agent_id"] = "11111111-2222-3333-4444-555555555555"
    flow["nodes"][2]["data"]["config"]["tool_id"] = "No Such Tool"
    wfr.resolve_entity_references(flow, db)
    configs = {n["id"]: n["data"]["config"] for n in flow["nodes"]}
    # UUID-ish id untouched; unknown name untouched (runtime reports at run time).
    assert configs["n2"]["agent_id"] == "11111111-2222-3333-4444-555555555555"
    assert configs["n3"]["tool_id"] == "No Such Tool"


def test_save_flow_ambiguous_entity_name_returns_flow_invalid(db):
    _mk_workflow(db)
    db.add(AgentApp(id="agent-a", name="Shared Name", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.add(AgentApp(id="agent-b", name="Shared Name", flow_mode=False, status="draft",
                    app_id="default-app", org_id="default-org", created_by_id="user-1"))
    db.commit()

    flow = _entity_ref_flow()
    flow["nodes"][1]["data"]["config"]["agent_id"] = "Shared Name"
    payload = {"action": "save_flow", "name": "Ambiguous", "flow": flow}
    out = wfr.save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert out["status"] == "flow_invalid"
    assert any("ambigu" in e.lower() for e in out["errors"])
